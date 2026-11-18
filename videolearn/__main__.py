import sys

from .cli import entry

if __name__ == "__main__":
    # `python -m videolearn web ...` —— 本机网页界面，不经过 argparse 的 run 分支，
    # 避免和 cli.py 的交互式参数解析纠缠（也便于另一个 agent 继续改 cli.py）。
    if sys.argv[1:2] == ["web"]:
        from .web import main as web_main

        raise SystemExit(web_main(sys.argv[2:]))
    entry()
