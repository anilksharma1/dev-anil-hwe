"""Put the package on sys.path so tests can `import pii_triage` without each file
doing its own sys.path surgery."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
