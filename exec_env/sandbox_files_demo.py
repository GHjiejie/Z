"""Demo 2：在宿主机和 Sandbox 之间上传、处理并下载文件。

运行：``uv run python -m exec_env.sandbox_files_demo``
"""

from pathlib import Path

from dotenv import load_dotenv
from langchain_daytona import DaytonaSandbox

from exec_env.sandbox_runtime import (
    HOST_REPORT_PATH,
    INPUT_PATH,
    INPUT_TEXT,
    create_sandbox_runtime,
    print_retained_sandbox,
)

PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(PROJECT_ENV_FILE, override=True)


def run_file_transfer_demo(backend: DaytonaSandbox) -> None:
    """上传文本，在 Sandbox 内处理，再下载生成的报告。"""
    print("\n[2] 上传文件、处理文件、下载产物")

    uploads = backend.upload_files([(INPUT_PATH, INPUT_TEXT)])
    for upload in uploads:
        if upload.error is not None:
            raise RuntimeError(f"上传 {upload.path} 失败：{upload.error}")

    command = f"""python - <<'PY'
from pathlib import Path

text = Path({INPUT_PATH!r}).read_text()
words = text.split()
report = f'lines={{len(text.splitlines())}}\\nwords={{len(words)}}\\n'
Path({HOST_REPORT_PATH!r}).write_text(report)
print(report, end='')
PY"""
    result = backend.execute(command)
    print(result.output)
    if result.exit_code != 0:
        raise RuntimeError("Sandbox 文件处理失败")

    downloads = backend.download_files([HOST_REPORT_PATH])
    for download in downloads:
        if download.content is None:
            raise RuntimeError(f"下载 {download.path} 失败：{download.error}")
        print(f"从 Sandbox 下载 {download.path}：")
        print(download.content.decode("utf-8"), end="")


def main() -> None:
    """创建独立 Sandbox 并运行文件传输示例。"""
    runtime = create_sandbox_runtime("files-demo")
    try:
        run_file_transfer_demo(runtime.backend)
    finally:
        print_retained_sandbox(runtime)


if __name__ == "__main__":
    main()
