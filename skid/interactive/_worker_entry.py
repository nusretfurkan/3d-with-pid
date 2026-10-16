"""Bootstrap for the generation worker under ``python -I``.

Isolated mode puts neither the working directory nor this script's directory
on ``sys.path``; only the checkout root containing ``skid`` is added here.
"""

import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from skid.interactive.worker import main

    sys.exit(main(sys.argv[1:]))
