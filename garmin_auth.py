"""One-time Garmin auth with MFA. Run interactively, then tokens are cached."""
import garth, os, configparser

config = configparser.ConfigParser()
config.read('config.ini')
username = config.get('Garmin', 'username')
password = config.get('Garmin', 'password')

tokendir = os.path.expanduser('~/.garmin_tokens')
os.makedirs(tokendir, exist_ok=True)

print(f'Logging in as {username}...')
garth.login(username, password)
garth.save(tokendir)
print('SUCCESS - tokens saved to ~/.garmin_tokens/')
