"""
Extracts ComfyUI generation metadata embedded in PNG files.

ComfyUI writes two text chunks into every PNG it saves:
  - "prompt":   the API-format graph (node_id -> {class_type, inputs}).
                Easiest to walk programmatically to pull out settings.
  - "workflow": the UI-format graph (nodes/links/groups as ComfyUI's editor
                stores it). This is what ComfyUI's "Load" menu expects, so
                it's what we hand back for the "copy/download workflow" button.

Some saver nodes (e.g. the LoRA Manager "Save Image" node, Civitai-style
savers) don't write the API-format "prompt" chunk at all. They write a
single A1111-style "parameters" text block instead (prompt, "Negative prompt:",
then a "Steps: ..., Sampler: ..., Seed: ..." settings line), usually alongside
the UI-format "workflow". When there's no "prompt" chunk we fall back to that
text - see parse_parameters_text(). It's deliberately preferred over trying to
read values out of the UI-format "workflow": that graph stores stale widget
defaults (an old seed, a leftover prompt, a 1024x1024 size) on nodes whose real
values arrive through links, so reading it naively gives confidently wrong answers.

Not every PNG will have these (e.g. a screenshot, or an image edited by
another tool that stripped metadata) - callers should handle has_metadata=False.
"""
import json
import re
from PIL import Image

# class_type names that hold the actual prompt text
TEXT_ENCODE_TYPES = {"CLIPTextEncode", "CLIPTextEncodeSDXL", "CLIPTextEncodeSDXLRefiner"}
# Input key names to try, in order, when pulling prompt text out of a node.
# Covers stock CLIPTextEncode ("text") as well as common custom prompt/text
# nodes (LoRA managers, prompt-combiner packs, etc.) that use different keys.
TEXT_INPUT_KEYS = ("text", "text_g", "prompt", "string", "positive", "negative")
SAMPLER_TYPES = {"KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced"}
CHECKPOINT_TYPES = {"CheckpointLoaderSimple", "CheckpointLoader", "UNETLoader"}
LORA_TYPES = {"LoraLoader", "LoraLoaderModelOnly", "LoraLoaderModelAndClip"}
LATENT_TYPES = {"EmptyLatentImage", "EmptySD3LatentImage"}
# Primitive value nodes - the literal is just sitting on their "value" input.
PRIMITIVE_TYPES = {"PrimitiveBoolean", "PrimitiveInt", "PrimitiveFloat", "PrimitiveString", "PrimitiveStringMultiline"}
# Nodes whose real output is computed at runtime (an LLM call, live data, etc.)
# and never written to the saved file, OR that explicitly represent "no text"
# (zeroing out a conditioning rather than passing anything through). Some of
# these hold a literal-looking string on an input like "prompt" or "text" -
# but that's a system prompt or seed fed *into* the dynamic step, not the
# thing it produced, and chasing a zeroed-out node's input just leads back to
# whatever it was zeroing, which is actively misleading to show as if it were
# real text. Resolving through any of these would return the wrong text with
# just as much confidence as the right text, so they're dead ends on purpose:
# this forces a switch node to fall back to its other branch instead of
# confidently returning something that only looks like the real prompt.
DYNAMIC_TYPES = {"TextGenerate", "ConditioningZeroOut"}
MAX_RESOLVE_DEPTH = 12


def read_png_text_chunks(filepath):
    """Return dict of PNG tEXt/iTXt chunk key->value for a file."""
    with Image.open(filepath) as img:
        return dict(img.info) if img.info else {}


def _is_link(value):
    """A ComfyUI 'prompt' input value that is a link looks like [node_id, output_index]."""
    return isinstance(value, list) and len(value) == 2


def _find_nodes_by_type(prompt, type_set):
    return [(nid, node) for nid, node in prompt.items() if node.get("class_type") in type_set]


def _resolve(prompt, value, depth=0):
    """Recursively resolve a raw input value down to a literal (str/int/float/
    bool), following whatever chain of nodes produced it. Real ComfyUI graphs
    route prompt text through all sorts of utility nodes before it reaches a
    sampler - A/B switches for trying alternate prompts, string-concatenation
    nodes for combining a base prompt with extra tags, reroute/preview
    passthroughs, primitive value nodes feeding several places at once. This
    walks through the common ones rather than giving up at the first hop.

    Deliberately best-effort, not a full graph executor: some branches (an
    LLM-based prompt-generation node, for instance) compute their output at
    runtime and never write it anywhere in the saved file - there's nothing
    to recover there. For a switch specifically, if the branch it's actually
    set to doesn't lead to a usable literal, the other branch is tried too;
    surfacing text you actually typed somewhere in the graph is more useful
    here than strictly honoring which side happened to be selected."""
    if depth > MAX_RESOLVE_DEPTH:
        return None

    if not _is_link(value):
        return value if isinstance(value, (str, int, float, bool)) else None

    node = prompt.get(str(value[0]))
    if not node:
        return None
    class_type = node.get("class_type", "")
    inputs = node.get("inputs", {})

    if class_type in DYNAMIC_TYPES:
        return None

    if class_type in PRIMITIVE_TYPES:
        return _resolve(prompt, inputs.get("value"), depth + 1)

    if "switch" in inputs and "on_true" in inputs and "on_false" in inputs:
        cond = _resolve(prompt, inputs.get("switch"), depth + 1)
        selected, other = (inputs.get("on_true"), inputs.get("on_false")) if cond \
            else (inputs.get("on_false"), inputs.get("on_true"))
        resolved = _resolve(prompt, selected, depth + 1)
        if isinstance(resolved, str) and resolved.strip():
            return resolved
        return _resolve(prompt, other, depth + 1)

    if class_type == "StringConcatenate":
        a = _resolve(prompt, inputs.get("string_a"), depth + 1)
        b = _resolve(prompt, inputs.get("string_b"), depth + 1)
        delim = _resolve(prompt, inputs.get("delimiter"), depth + 1)
        delim = delim if isinstance(delim, str) else ""
        parts = [str(p) for p in (a, b) if isinstance(p, (str, int, float)) and str(p).strip()]
        return delim.join(parts) if parts else None

    for key in TEXT_INPUT_KEYS:
        if key in inputs:
            resolved = _resolve(prompt, inputs[key], depth + 1)
            if isinstance(resolved, str) and resolved.strip():
                return resolved

    # Single-input wrapper (Reroute, PreviewAny, and similar passthrough
    # nodes we don't know by name) - just follow the one thing feeding it.
    if len(inputs) == 1:
        return _resolve(prompt, next(iter(inputs.values())), depth + 1)

    candidates = []
    for v in inputs.values():
        resolved = _resolve(prompt, v, depth + 1)
        if isinstance(resolved, str) and resolved.strip():
            candidates.append(resolved)
    return max(candidates, key=len) if candidates else None


def parse_prompt_graph(prompt):
    """Walk the API-format 'prompt' graph and pull out the fields we care about.
    Returns a dict of extracted fields. Falls back gracefully if the graph
    doesn't match expected shapes (custom/unusual workflows)."""
    result = {
        "positive_prompt": None,
        "negative_prompt": None,
        "model_name": None,
        "sampler_name": None,
        "scheduler": None,
        "seed": None,
        "steps": None,
        "cfg": None,
        "denoise": None,
        "width": None,
        "height": None,
        "loras": [],
    }

    samplers = _find_nodes_by_type(prompt, SAMPLER_TYPES)
    if samplers:
        # If there are multiple samplers (e.g. a refiner pass), use the last one -
        # it's the one that produced the final image in most standard workflows.
        _, sampler_node = samplers[-1]
        inputs = sampler_node.get("inputs", {})

        result["seed"] = inputs.get("seed") or inputs.get("noise_seed")
        result["steps"] = inputs.get("steps")
        result["cfg"] = inputs.get("cfg")
        result["sampler_name"] = inputs.get("sampler_name")
        result["scheduler"] = inputs.get("scheduler")
        result["denoise"] = inputs.get("denoise")

        pos_node = _resolve(prompt, inputs.get("positive"))
        neg_node = _resolve(prompt, inputs.get("negative"))
        if isinstance(pos_node, str) and pos_node.strip():
            result["positive_prompt"] = pos_node
        if isinstance(neg_node, str) and neg_node.strip():
            result["negative_prompt"] = neg_node

    # Fallback: for whichever side we still couldn't trace through a sampler
    # (no sampler found, or an unresolvable link), grab from plain
    # CLIPTextEncode nodes found anywhere in the graph (better than nothing).
    # Handled per-field, since a workflow might resolve one side via a custom
    # node (see above) and need this fallback only for the other.
    if result["positive_prompt"] is None or result["negative_prompt"] is None:
        text_nodes = _find_nodes_by_type(prompt, TEXT_ENCODE_TYPES)
        texts = []
        for _, n in text_nodes:
            resolved = _resolve(prompt, n.get("inputs", {}).get("text"))
            if isinstance(resolved, str) and resolved.strip():
                texts.append(resolved)
        if result["positive_prompt"] is None and len(texts) >= 1:
            result["positive_prompt"] = texts[0]
        if result["negative_prompt"] is None and len(texts) >= 2:
            result["negative_prompt"] = texts[1]
        elif result["negative_prompt"] is None and len(texts) == 1:
            # Only one plain CLIPTextEncode in the whole graph and we still need
            # a negative - it's almost certainly not also the positive, so leave
            # negative blank rather than guessing wrong.
            pass

    # Belt-and-suspenders: whatever path set these, guarantee they're either
    # real text or None before this leaves the function. A non-string value
    # here would otherwise reach the database and fail to save at all.
    if not isinstance(result["positive_prompt"], str):
        result["positive_prompt"] = None
    if not isinstance(result["negative_prompt"], str):
        result["negative_prompt"] = None

    checkpoints = _find_nodes_by_type(prompt, CHECKPOINT_TYPES)
    if checkpoints:
        _, ckpt_node = checkpoints[0]
        ckpt_inputs = ckpt_node.get("inputs", {})
        result["model_name"] = ckpt_inputs.get("ckpt_name") or ckpt_inputs.get("unet_name")

    for _, lora_node in _find_nodes_by_type(prompt, LORA_TYPES):
        li = lora_node.get("inputs", {})
        result["loras"].append({
            "name": li.get("lora_name"),
            "strength_model": li.get("strength_model"),
            "strength_clip": li.get("strength_clip"),
        })

    latents = _find_nodes_by_type(prompt, LATENT_TYPES)
    if latents:
        _, latent_node = latents[0]
        li = latent_node.get("inputs", {})
        result["width"] = li.get("width")
        result["height"] = li.get("height")

    # Same protection as above, extended to every other field: any of these
    # can also come from a wired-in primitive/switch node instead of a typed
    # value, which would show up here as a list/dict rather than a plain
    # value - and just as unsavable. Loras is deliberately excluded since it's
    # always a list of dicts by design and gets JSON-encoded before storage.
    for key in ("model_name", "sampler_name", "scheduler"):
        if result[key] is not None and not isinstance(result[key], str):
            result[key] = None
    for key in ("seed", "steps", "width", "height"):
        if result[key] is not None and not isinstance(result[key], int):
            result[key] = None
    for key in ("cfg", "denoise"):
        if result[key] is not None and not isinstance(result[key], (int, float)):
            result[key] = None

    return result


# Scheduler names that saver nodes tack onto a sampler name ("Euler simple",
# "DPM++ 2M Karras"). A1111-style text has no separate scheduler field in
# older versions, so a recognised trailing word gets split back out.
KNOWN_SCHEDULERS = {
    "simple", "normal", "karras", "exponential", "sgm_uniform", "ddim_uniform",
    "beta", "linear_quadratic", "kl_optimal", "bong_tangent", "align_your_steps",
    "polyexponential", "uniform",
}

# <lora:name:weight> or <lora:name:model_weight:clip_weight>
_LORA_TAG_RE = re.compile(r"<lora:([^:>]+):([^:>]+)(?::([^>]+))?>")


def _grab_setting(settings_line, key):
    """Pull one 'Key: value' out of an A1111 settings line. Values can't be
    split on every comma - Hashes and Civitai resources hold JSON full of
    them - so this anchors on the key and reads up to the next comma, which
    is right for every simple value (numbers, names, WxH sizes)."""
    m = re.search(r"(?:^|, )" + re.escape(key) + r": ([^,]+)", settings_line)
    return m.group(1).strip() if m else None


def _to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_parameters_text(text):
    """Parse an A1111-style "parameters" text block into the same dict shape
    parse_prompt_graph() returns. Returns None if the text doesn't look like
    one (no settings line and no 'Negative prompt:' marker), so callers can
    fall back to whatever they did before."""
    if not isinstance(text, str) or not text.strip():
        return None

    lines = text.strip().split("\n")

    # The settings line is the LAST line starting with "Steps:" - searched
    # from the end so a prompt that happens to contain such a line can't
    # be mistaken for it.
    settings_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("Steps:"):
            settings_idx = i
            break
    settings = lines[settings_idx] if settings_idx is not None else ""
    body = "\n".join(lines[:settings_idx] if settings_idx is not None else lines)

    if body.startswith("Negative prompt:"):
        pos_text, neg_text = "", body[len("Negative prompt:"):]
    elif "\nNegative prompt:" in body:
        pos_text, neg_text = body.split("\nNegative prompt:", 1)
    else:
        pos_text, neg_text = body, ""

    if settings_idx is None and "Negative prompt:" not in body:
        return None  # just some other text chunk, not generation parameters

    # LoRA tags ride along inside the prompt text. Pull them out (they're
    # reported separately, not part of what the person actually typed).
    loras = []
    for name, weight, clip_weight in _LORA_TAG_RE.findall(pos_text):
        strength_model = _to_float(weight)
        strength_clip = _to_float(clip_weight) if clip_weight else strength_model
        loras.append({
            "name": name.strip(),
            "strength_model": strength_model,
            "strength_clip": strength_clip,
        })
    pos_text = _LORA_TAG_RE.sub("", pos_text)
    neg_text = _LORA_TAG_RE.sub("", neg_text)

    def _clean(t):
        t = re.sub(r"\n{3,}", "\n\n", t.strip())
        return t or None

    # No tags (older saves, or a saver with that option off): the Hashes
    # block still names every LoRA used, just without weights. Better to
    # list them with the weight marked unknown than to show nothing.
    if not loras:
        hashes_match = re.search(r"Hashes: (\{.*?\})(?:, |$)", settings)
        if hashes_match:
            try:
                hashes = json.loads(hashes_match.group(1))
                for key in hashes:
                    if key.startswith("LORA:"):
                        loras.append({
                            "name": key[len("LORA:"):],
                            "strength_model": None,
                            "strength_clip": None,
                        })
            except (json.JSONDecodeError, TypeError):
                pass

    sampler = _grab_setting(settings, "Sampler")
    scheduler = _grab_setting(settings, "Schedule type")
    if scheduler:
        scheduler = scheduler.lower().replace(" ", "_")
    elif sampler:
        words = sampler.split()
        if len(words) > 1 and words[-1].lower() in KNOWN_SCHEDULERS:
            scheduler = words[-1].lower()
            sampler = " ".join(words[:-1])

    width = height = None
    size = _grab_setting(settings, "Size")
    if size and re.fullmatch(r"\d+x\d+", size):
        width, height = (int(n) for n in size.split("x"))

    steps = _grab_setting(settings, "Steps")
    seed = _grab_setting(settings, "Seed")

    return {
        "positive_prompt": _clean(pos_text),
        "negative_prompt": _clean(neg_text),
        "model_name": _grab_setting(settings, "Model"),
        "sampler_name": sampler,
        "scheduler": scheduler,
        "seed": int(seed) if seed and seed.isdigit() else None,
        "steps": int(steps) if steps and steps.isdigit() else None,
        "cfg": _to_float(_grab_setting(settings, "CFG scale")),
        "denoise": _to_float(_grab_setting(settings, "Denoising strength")),
        "width": width,
        "height": height,
        "loras": loras,
    }


def prompt_from_workflow(raw_workflow):
    """Last-resort prompt lookup in the UI 'workflow' graph, for savers that
    write the 'parameters' text but leave its prompt empty (the LoRA Manager
    Save Image node does this when the prompt reaches the sampler through a
    subgraph rather than through its own text box).

    Only the workflow's top-level node instances are read - never the
    subgraph definitions - because an instance's widget values are what was
    actually set for the run; the definitions just hold stale defaults and
    example text. Notes, LoRA stack text and file names are skipped, and the
    longest remaining string is taken as the prompt, since prompt text is
    reliably far longer than any other setting. Active LoRA Manager trigger
    words are appended, matching what was actually sent to the model.
    Returns None when nothing plausible is found."""
    try:
        wf = json.loads(raw_workflow)
        nodes = wf.get("nodes") or []
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None

    best = None
    triggers = []
    for node in nodes:
        if not isinstance(node, dict) or node.get("mode") in (2, 4):  # muted / bypassed
            continue
        ntype = str(node.get("type") or "")
        if "Note" in ntype:
            continue
        values = node.get("widgets_values")
        if not isinstance(values, list):
            continue
        if "TriggerWord" in ntype:
            words = next((v for v in reversed(values) if isinstance(v, str)), "")
            if words.strip():
                triggers.append(words.strip())
            continue
        for v in values:
            if (isinstance(v, str) and len(v.strip()) >= 20 and "<lora:" not in v
                    and not v.strip().lower().endswith((".safetensors", ".ckpt", ".pt", ".gguf"))):
                if best is None or len(v) > len(best):
                    best = v
    if not best:
        return None
    text = best.strip()
    for words in triggers:
        if words.rstrip(", ") not in text:
            text = text.rstrip(", ") + ", " + words
    return text


def extract_metadata(filepath):
    """Main entry point. Reads a PNG file and returns a dict ready to insert
    into the images table. Never raises - if there's no/invalid metadata,
    returns has_metadata=False with everything else empty."""
    base = {
        "has_metadata": False,
        "prompt_json": None,
        "workflow_json": None,
        "positive_prompt": None,
        "negative_prompt": None,
        "model_name": None,
        "sampler_name": None,
        "scheduler": None,
        "seed": None,
        "steps": None,
        "cfg": None,
        "denoise": None,
        "width": None,
        "height": None,
        "loras": [],
    }

    try:
        chunks = read_png_text_chunks(filepath)
    except Exception:
        return base

    raw_prompt = chunks.get("prompt")
    raw_workflow = chunks.get("workflow")

    if raw_workflow:
        base["workflow_json"] = raw_workflow
    if raw_prompt:
        base["prompt_json"] = raw_prompt

    if not raw_prompt:
        # No API-format graph to walk. Some savers write an A1111-style
        # "parameters" text block instead - use that if it's there.
        # (workflow_json may still be present and usable for copy/download.)
        try:
            parsed = parse_parameters_text(chunks.get("parameters"))
        except Exception:
            parsed = None  # never let an odd text block break an upload
        if parsed:
            base.update(parsed)
            base["has_metadata"] = True
            if not base["positive_prompt"] and raw_workflow:
                try:
                    base["positive_prompt"] = prompt_from_workflow(raw_workflow)
                except Exception:
                    pass
            return base
        base["has_metadata"] = bool(raw_workflow)
        return base

    try:
        prompt_graph = json.loads(raw_prompt)
    except (json.JSONDecodeError, TypeError):
        base["has_metadata"] = bool(raw_workflow)
        return base

    try:
        extracted = parse_prompt_graph(prompt_graph)
    except Exception:
        # However unusual the graph shape, a failure here should never break
        # the upload - the raw JSON is already saved above either way.
        base["has_metadata"] = True
        return base

    base.update(extracted)
    base["has_metadata"] = True
    return base


def get_full_workflow_json(image_row):
    """Given an images table row, return the best available full workflow
    JSON string for copy/download - prefer the UI 'workflow' format since
    that's what ComfyUI's Load menu expects; fall back to the API 'prompt' format."""
    if image_row["workflow_json"]:
        return image_row["workflow_json"]
    if image_row["prompt_json"]:
        return image_row["prompt_json"]
    return None
