"""Interactive initialization; never writes plaintext passwords to output."""
import getpass
import secrets
from config import STATE
from console_app import initialize_auth


def main():
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not (STATE/'console-auth.json').exists():
        password=getpass.getpass('Administrator password (12+ characters): ')
        if len(password)<12:raise ValueError('Use at least 12 characters')
        if getpass.getpass('Repeat administrator password: ')!=password:raise ValueError('Passwords differ')
        initialize_auth(STATE,'admin',password)
    if not (STATE/'api-key').exists():
        key=getpass.getpass('Existing API key to preserve (leave blank for a random initial key): ') or secrets.token_urlsafe(32)
        with open(STATE/'api-key','w') as stream:
            (STATE/'api-key').chmod(0o600);stream.write(key)
    print('Initialized. Log in to the console to create and copy client keys.')


if __name__=='__main__':main()
