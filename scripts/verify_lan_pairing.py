"""验证局域网的**项目级**配对流程（真实网络请求，不是单元测试模拟）。

脚本会：
1. 在临时数据目录里启动一个监听 0.0.0.0 的服务；
2. 用**回环地址**模拟桌面端（建项目、取配对码、换码）；
3. 用**局域网地址**模拟手机端（无凭证 → 401；兑换 → 会话；访问别的项目 → 403；
   项目列表被收窄；换码后旧会话失效）。

用法：
    python scripts/verify_lan_pairing.py
    python scripts/verify_lan_pairing.py --host 192.168.1.10   # 指定本机的局域网地址
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pick_lan_address() -> str:
    """找一个非回环的 IPv4 地址。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def call(url: str, method: str = "GET", data=None, headers=None):
    req = urllib.request.Request(url, method=method)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, body, timeout=15) as response:
            raw = response.read()
            return response.status, json.loads(raw or b"null")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"null")
        except Exception:  # noqa: BLE001
            return exc.code, {"raw": raw[:200].decode("utf-8", "replace")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--host", default="", help="本机的局域网地址（默认自动探测）")
    parser.add_argument("--keep", action="store_true", help="保留临时数据目录")
    args = parser.parse_args()

    lan_host = args.host or pick_lan_address()
    data_dir = ROOT / "data" / "lan-pairing-check"
    shutil.rmtree(data_dir, ignore_errors=True)
    env = dict(os.environ)
    env["GODFATHER_DATA_DIR"] = str(data_dir)
    proc = subprocess.Popen(
        [sys.executable, "-m", "godfather", "serve", "--host", "0.0.0.0", "--port", str(args.port)],
        env=env,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    local_base = f"http://127.0.0.1:{args.port}"
    lan_base = f"http://{lan_host}:{args.port}"

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'✓' if ok else '✗'} {label}{('  ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    try:
        time.sleep(7)
        print(f"局域网地址：{lan_host}")

        print("\n─── 桌面端（回环）───")
        _, project_a = call(
            f"{local_base}/api/projects", "POST", {"name": "机密项目A", "taskpack": "markdown_to_latex"}
        )
        _, project_b = call(
            f"{local_base}/api/projects", "POST", {"name": "机密项目B", "taskpack": "markdown_to_latex"}
        )
        check("创建两个项目", bool(project_a.get("id")) and bool(project_b.get("id")))
        status, info = call(f"{local_base}/api/system/pairing?project_id={project_a['id']}")
        code = info.get("code", "")
        check("桌面端能取到项目 A 的配对码", bool(code), f"code={code} 有效期 {info.get('expires_in')}s")

        print("\n─── 手机端（局域网）───")
        status, body = call(f"{lan_base}/api/projects")
        check("无凭证访问被拒", status == 401 and body.get("code") == "pairing_required", f"HTTP {status}")

        status, body = call(f"{lan_base}/api/system/pairing?project_id={project_a['id']}")
        check("局域网取不到配对码", status == 200 and not body.get("code"))

        status, session = call(
            f"{lan_base}/api/system/pairing/redeem", "POST", {"code": code}
        )
        token = session.get("token", "")
        check(
            "用配对码换到项目级会话",
            status == 200 and session.get("project_id") == project_a["id"] and bool(token),
            f"解锁 {session.get('project_name')}",
        )

        headers = {"X-Godfather-Session": token}
        status, body = call(f"{lan_base}/api/projects/{project_a['id']}", headers=headers)
        check("会话能访问自己的项目", status == 200 and body.get("name") == "机密项目A")

        status, body = call(f"{lan_base}/api/projects/{project_b['id']}", headers=headers)
        check(
            "会话访问别的项目被拒",
            status == 403 and body.get("code") == "scope_mismatch",
            f"HTTP {status}",
        )

        status, body = call(f"{lan_base}/api/projects", headers=headers)
        names = [row.get("name") for row in body] if isinstance(body, list) else []
        check("项目列表被收窄到自己的项目", names == ["机密项目A"], f"看到 {names}")

        # 配对码是一次性的（兑换即作废），所以这里要用一个新签发的码
        _, fresh = call(f"{local_base}/api/system/pairing?project_id={project_a['id']}")
        fresh_code = fresh.get("code", "")
        status, body = call(
            f"{lan_base}/api/projects/{project_a['id']}", headers={"X-Godfather-Pairing": fresh_code}
        )
        check("配对码本身也能当凭证用（方便 curl）", status == 200)

        status, body = call(
            f"{lan_base}/api/system/pairing/redeem", "POST", {"code": code}
        )
        check("配对码一次性（兑换后立刻作废）", status == 401)

        status, body = call(
            f"{lan_base}/api/projects", "POST", {"name": "偷偷建的项目", "taskpack": "markdown_to_latex"},
            headers=headers,
        )
        check("会话不能新建项目", status == 403, f"HTTP {status}")

        print("\n─── 桌面端换码 ──")
        status, rotated = call(
            f"{local_base}/api/system/pairing/rotate", "POST", {"project_id": project_a["id"]}
        )
        check("重新生成配对码", status == 200 and rotated.get("code") != code)

        status, body = call(f"{lan_base}/api/projects/{project_a['id']}", headers=headers)
        check("换码后旧会话立即失效", status == 401, f"HTTP {status}")

        status, body = call(f"{local_base}/api/projects")
        check("桌面端（回环）不受影响", status == 200 and isinstance(body, list))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)

    print()
    if failures:
        print(f"✗ 有 {len(failures)} 项未通过：")
        for item in failures:
            print(f"   - {item}")
        return 1
    print("✓ 项目级配对流程全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
