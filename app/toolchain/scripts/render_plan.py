"""Retired export entry point. Native MP4 export is not connected."""
import json
import sys


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    print(json.dumps({
        "status": "unsupported",
        "code": "NATIVE_MP4_EXPORT_NOT_CONNECTED",
        "message": "This old export entry point is retired. Native draft editing and frame previews are available in separate tools; native MP4 export is not connected. No external rendering fallback is permitted.",
        "output_created": False,
    }), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
