"""Build and verify tool artifacts without connecting to an application database."""

import argparse
import json
from pathlib import Path

from agent_permissions.api import create_app
from agent_permissions.tooling import compile_tools
from api_tools.compiler import ContractError, canonical, compare_bundles, verify_bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成并验证 API Agent 工具包")
    parser.add_argument("command", choices=["build", "check", "diff"])
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("agent_permissions/build/tools.manifest.json"),
    )
    parser.add_argument("--baseline", type=Path, help="上一发布版本的工具包")
    parser.add_argument("--compatibility", type=Path, help="绑定两版摘要的兼容变更声明")
    parser.add_argument(
        "--allow-breaking", action="store_true", help="显式接受差异报告中的破坏性变更"
    )
    args = parser.parse_args(argv)
    try:
        expected = compile_tools(create_app())
        if args.command == "check":
            candidate = json.loads(args.output.read_text())
            verify_bundle(candidate)
            if canonical(candidate) != canonical(expected):
                raise ContractError("工具包与当前 API 不匹配，请重新生成")
        if args.baseline:
            baseline = json.loads(args.baseline.read_text())
            verify_bundle(baseline)
            report = compare_bundles(baseline, expected)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            accepted = args.allow_breaking
            if args.compatibility:
                decision = json.loads(args.compatibility.read_text())
                accepted = (
                    decision.get("baselineDigest") == baseline["bundleDigest"]
                    and decision.get("candidateDigest") == expected["bundleDigest"]
                    and sorted(decision.get("breakingTools", [])) == report["breaking"]
                    and isinstance(decision.get("reason"), str)
                    and bool(decision["reason"].strip())
                )
            if report["breaking"] and not accepted:
                print("存在不兼容变化；请处理兼容版本，或显式声明本次发布接受该变化。")
                return 1
        elif args.command == "diff":
            raise ContractError("diff 需要 --baseline")
        if args.command == "build":
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix(args.output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n"
            )
            temporary.replace(args.output)
        print(
            f"{args.command}: {len(expected['tools'])} tools, revision={expected['bundleDigest']}"
        )
        return 0
    except ContractError as error:
        print(f"工具契约校验失败：{error}")
        return 1
    except (OSError, ValueError, KeyError):
        print("工具包生成或校验失败，请检查文件路径与工具包格式。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
