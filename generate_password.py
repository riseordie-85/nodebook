"""
Run this once to generate the APP_PASSWORD_HASH value for your .env file:

    python3 generate_password.py

It will prompt you for a password and print the hash to paste into .env.
"""
import getpass
from werkzeug.security import generate_password_hash

if __name__ == "__main__":
    pw = getpass.getpass("Choose a password: ")
    confirm = getpass.getpass("Confirm password: ")
    if pw != confirm:
        print("Passwords didn't match, try again.")
    else:
        print("\nAdd this line to your .env file:\n")
        print(f"APP_PASSWORD_HASH={generate_password_hash(pw)}")
