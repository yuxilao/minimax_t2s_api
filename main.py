#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import os
import sys

from t2s_tool import paths
from t2s_tool.config import load_config
from t2s_tool.errors import APIError, CancelledError, ConfigError, FileError, ParseError
from t2s_tool.pipeline import run_job


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="语音目录批量转语音包工具（MiniMax 异步 TTS，并发逐条合成）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python main.py "9国语种...xlsx"                      # 全部语言，自动识别解析器
  python main.py list.xlsx --language 俄语 --language 日语
  python main.py list.xlsx --provider fake --output /tmp/out   # 离线演示
""",
    )
    ap.add_argument("input_file", help="语音目录文件（如 9国语种 xlsx）")
    ap.add_argument("--parser", default="auto", help="解析器名或 auto 自动检测（默认 auto）")
    ap.add_argument("--language", action="append", dest="languages", default=None,
                    help="只转换指定语言，可多次指定；默认全部语言")
    ap.add_argument("--provider", default=None, help="供应商（默认取配置 provider；离线演示用 fake）")
    ap.add_argument("--config", default=paths.default_config_path(), help="配置文件路径")
    ap.add_argument("--output", default=None, help="输出目录（默认 <输入文件所在目录>/语音包）")
    args = ap.parse_args(argv)

    try:
        config = load_config(args.config)
        output = args.output or os.path.join(os.path.dirname(os.path.abspath(args.input_file)), "语音包")
        state = {"poll": False}

        def cli_on_event(ev):
            line = f"[{ev.stage}] {ev.message}"
            if ev.stage == "poll":
                # 轮询进度同一行原地刷新，不刷屏
                print("\r" + line.ljust(40), end="", flush=True)
                state["poll"] = True
            else:
                if state["poll"]:
                    print()
                    state["poll"] = False
                print(line, flush=True)

        report = run_job(
            input_path=args.input_file,
            parser_name=args.parser,
            languages=args.languages,
            provider_name=args.provider,
            config=config,
            output_dir=output,
            on_event=cli_on_event,
        )
    except CancelledError as e:
        print(f"\n已取消: {e}", file=sys.stderr)
        return 130
    except (ConfigError, ParseError, FileError, APIError) as e:
        print(f"\n错误: {e}", file=sys.stderr)
        return 1

    exit_code = 0
    print("\n===== 转换报告 =====")
    for rep in report.language_reports:
        print(f"{rep.language}: 成功 {len(rep.success_ids)}，缺失 {len(rep.missing_ids)}，未知 {len(rep.unknown_ids)}")
        if rep.output_dir:
            print(f"  输出目录: {rep.output_dir}")
            print(f"  语音包: {rep.tar_path}")
        else:
            print("  全部缺失，未生成语音包")
        if rep.missing_ids:
            exit_code = 1
            print(f"  缺失ID: {rep.missing_ids}")
        for w in rep.warnings:
            print(f"  警告: {w}")
    if report.parse_warnings:
        print(f"解析警告 {len(report.parse_warnings)} 条（前 10 条）:")
        for w in report.parse_warnings[:10]:
            print(f"  {w}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
