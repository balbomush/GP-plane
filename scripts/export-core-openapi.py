"""Export the live Core schema and check packaged Swagger resources."""

import json
from pathlib import Path
import sys

from gp_control_plane.http.api_v2.app import verify_package


if __name__ == "__main__":
    schema = verify_package()
    Path(sys.argv[1]).write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
