"""Console executable entry point; keep stdout available to automation."""
from wechat_export_tool.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
