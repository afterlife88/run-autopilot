"""
Shared config access: filesystem paths and home coordinates.

Everything falls back to the original hardcoded values, so an unmodified
config.ini keeps working; open-source users override via [Paths]/[Settings].
"""
import configparser
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")

_PATH_DEFAULTS = {
    "tracks_dir": "/home/pi/fitness-dashboard/public/tracks",
    "dashboard_dir": "/home/pi/fitness-dashboard",
}


def config():
    c = configparser.ConfigParser()
    c.read(CONFIG_PATH)
    return c


def path(key):
    c = config()
    return os.path.expanduser(c.get("Paths", key, fallback=_PATH_DEFAULTS[key]))


def home_coords():
    """Fallback coordinates when a run has no GPS (weather lookups)."""
    c = config()
    lat = c.getfloat("Settings", "home_lat", fallback=55.6761)
    lon = c.getfloat("Settings", "home_lon", fallback=12.5683)
    return lat, lon
