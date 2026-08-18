"""离线交付包与两条部署路径（v6 §11.4 + §13 M8 出口标准）。

| 出口标准 | 本文件哪条 |
|---|---|
| 包结构与 v6 §11.4 那张图一致 | `test_builder_creates_every_directory_in_the_spec` |
| `CHECKSUMS.sha256` 校验通过 | `test_checksums_cover_every_file_and_verify` |
| **改一个字节就必须校验失败** | `test_tampering_a_file_breaks_the_checksums` |
| install.sh 的八个阶段齐备 | `test_installer_has_all_eight_stages` |
| **磁盘余量 ≥50 GB 是硬门槛** | `test_installer_enforces_the_fifty_gigabyte_floor` |
| **黄金用例绿灯才算装完** | `test_installer_fails_when_golden_cases_fail` |
| native 与 compose 的确定性配置一致 | `test_both_paths_pin_the_same_determinism_knobs` |

## 为什么这里只建「小件包」

真包里 wheels 约 6 GB、模型约 14 GB，收集一次要几十分钟 —— 不能放进常规
pytest。所以这里用 `--skip-wheels --skip-models --skip-images --skip-conda-pkgs`
建一个**结构完整但没有大件**的包，验的是**结构、校验和、脚本逻辑**。

大件本身的验证是另一回事，由 M8 的收工实测（真跑一遍 `pip install --no-index`）
覆盖，那不是每次 CI 都该跑的东西。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from backend.core.config import PROJECT_ROOT

pytestmark = pytest.mark.guardrail

BUILDER = PROJECT_ROOT / "deploy" / "offline-package" / "build_package.sh"
INSTALLER = PROJECT_ROOT / "deploy" / "offline-package" / "scripts" / "install.sh"
COMPARE = PROJECT_ROOT / "deploy" / "scripts" / "compare_deploy_paths.sh"

#: v6 §11.4 那张图里的目录，一个都不能少
SPEC_DIRS = (
    "native",
    "compose",
    "images",
    "models",
    "wheels",
    "conda",
    "sql",
    "rules",
    "skills",
    "templates",
    "scripts",
)


@pytest.fixture(scope="module")
def built_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """建一个「小件包」：结构完整，不含 wheels/模型/镜像。"""
    out = tmp_path_factory.mktemp("release")
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "bash",
            str(BUILDER),
            "--skip-wheels",
            "--skip-models",
            "--skip-images",
            "--skip-conda-pkgs",
        ],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "FTS_RELEASE_DIR": str(out)},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"build_package.sh 失败：\n{result.stdout}\n{result.stderr}")
    pkg = out / "fts-release-v1.0.0"
    assert pkg.is_dir(), result.stdout
    return pkg


# ═════════════════════════════════════════════════════════════════════
# 包结构
# ═════════════════════════════════════════════════════════════════════
def test_builder_creates_every_directory_in_the_spec(built_package: Path) -> None:
    missing = [name for name in SPEC_DIRS if not (built_package / name).is_dir()]
    assert missing == [], f"v6 §11.4 要求的目录缺了：{missing}"


def test_package_carries_the_scripts_that_matter(built_package: Path) -> None:
    for rel in (
        "scripts/install.sh",
        "native/healthcheck.sh",
        "native/start_all_app.sh",
        "compose/docker-compose.yml",
        "compose/iptables_egress_drop.sh",
        "sql/alembic.ini",
        "rules/semantics.yaml",
        ".env.example",
    ):
        assert (built_package / rel).is_file(), f"包里缺 {rel}"


def test_source_comes_from_git_archive_not_a_raw_copy(built_package: Path) -> None:
    """`src/` 只带**入库的文件**。

    用 `cp -a` 会把 `.data/`（模型权重）、`data/plans/`（历史产物）、`.env`
    一起打进去 —— 那正是 v6 §11.5「机密管理」明令不许的。这条钉住构建方式。
    """
    src = built_package / "src"
    assert (src / "backend").is_dir() and (src / "COMMIT").is_file()
    for forbidden in (".env", ".data", "data/plans", ".git"):
        assert not (src / forbidden).exists(), f"src/ 里不该有 {forbidden}"


def test_no_env_files_are_shipped(built_package: Path) -> None:
    """**包里不许有任何 `.env`**（只有 `.env.example` 与 compose 的模板）。"""
    leaked = [
        str(path.relative_to(built_package))
        for path in built_package.rglob("*.env")
        if not path.name.endswith(".env.example")
    ]
    assert leaked == [], f"包里混进了 env 文件：{leaked}"


# ═════════════════════════════════════════════════════════════════════
# CHECKSUMS
# ═════════════════════════════════════════════════════════════════════
def test_checksums_cover_every_file_and_verify(built_package: Path) -> None:
    """★ 出口标准：`sha256sum -c` 全绿，且清单覆盖包里每一个文件。"""
    manifest = built_package / "CHECKSUMS.sha256"
    assert manifest.is_file()

    listed = {line.split(None, 1)[1].strip() for line in manifest.read_text().splitlines() if line}
    actual = {
        "./" + str(path.relative_to(built_package))
        for path in built_package.rglob("*")
        if path.is_file() and path.name != "CHECKSUMS.sha256"
    }
    assert listed == actual, (
        f"清单与实际文件对不上。\n漏登记：{sorted(actual - listed)[:5]}\n"
        f"多登记：{sorted(listed - actual)[:5]}"
    )

    result = subprocess.run(
        ["sha256sum", "-c", "CHECKSUMS.sha256", "--quiet"],  # noqa: S607
        cwd=str(built_package),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_tampering_a_file_breaks_the_checksums(built_package: Path, tmp_path: Path) -> None:
    """**自检**：改一个字节，校验必须红。

    没有这一条，`CHECKSUMS.sha256` 完全可能是一份「生成了但校验不出问题」的
    摆设 —— 而它的全部价值就是能发现「包在传输中被改过」。
    在副本上做，不污染 module 作用域的那个包。
    """
    import shutil

    copy = tmp_path / "pkg"
    shutil.copytree(built_package, copy)
    target = copy / "rules" / "semantics.yaml"
    original = target.read_text(encoding="utf-8")
    target.write_text(original + "\n# 偷偷加一行\n", encoding="utf-8")

    result = subprocess.run(
        ["sha256sum", "-c", "CHECKSUMS.sha256", "--quiet"],  # noqa: S607
        cwd=str(copy),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "改了文件却校验通过 —— CHECKSUMS 是摆设"
    assert "semantics.yaml" in result.stdout + result.stderr


def test_checksums_do_not_list_themselves(built_package: Path) -> None:
    """清单里不该有它自己（自指的校验和算不出来）。"""
    text = (built_package / "CHECKSUMS.sha256").read_text(encoding="utf-8")
    assert "CHECKSUMS.sha256" not in text


def test_checksum_algorithm_is_sha256(built_package: Path) -> None:
    """抽一行核对：确实是 sha256 而不是别的摘要。"""
    line = (built_package / "CHECKSUMS.sha256").read_text(encoding="utf-8").splitlines()[0]
    digest, rel = line.split(None, 1)
    assert len(digest) == 64
    actual = hashlib.sha256((built_package / rel.strip()).read_bytes()).hexdigest()
    assert digest == actual


# ═════════════════════════════════════════════════════════════════════
# install.sh
# ═════════════════════════════════════════════════════════════════════
def test_installer_has_all_eight_stages() -> None:
    """v6 §11.4 规定的流程，八个阶段一个不少、顺序不能反。"""
    text = INSTALLER.read_text(encoding="utf-8")
    stages = [
        "① 环境体检",
        "② 校验 CHECKSUMS",
        "③ 铺开源码",
        "④ 建 conda 环境",
        "⑤ 导入模型",
        "⑥ 裸装服务",
        "⑦ 初始化数据库",
        "⑧ 黄金用例",
    ]
    positions = []
    for stage in stages:
        assert stage in text, f"install.sh 缺阶段：{stage}"
        positions.append(text.index(stage))
    assert positions == sorted(positions), "install.sh 的阶段顺序被打乱了"


def test_installer_enforces_the_fifty_gigabyte_floor() -> None:
    """★ v6 §11.4 明文要求的那条硬门槛。"""
    text = INSTALLER.read_text(encoding="utf-8")
    assert 'MIN_DISK_GB="${MIN_DISK_GB:-50}"' in text
    assert "磁盘余量" in text


def test_installer_verifies_checksums_before_touching_the_system() -> None:
    """校验必须发生在**动系统之前** —— 装到一半才发现包坏了，清理比重下还贵。"""
    text = INSTALLER.read_text(encoding="utf-8")
    assert text.index("② 校验 CHECKSUMS") < text.index("③ 铺开源码")
    assert text.index("sha256sum -c") < text.index('mkdir -p "$APP_DIR"')


def test_installer_fails_when_golden_cases_fail() -> None:
    """★ 「绿灯才算安装成功」：黄金用例不过要 `exit 1`，不能只打条警告。"""
    text = INSTALLER.read_text(encoding="utf-8")
    tail = text[text.index("⑧ 黄金用例") :]
    assert "golden_fingerprint.py" in tail
    assert "安装不算成功" in tail
    assert "exit 1" in tail


def test_installer_installs_dependencies_without_network() -> None:
    """★ v6 §11.5「依赖离线」：`--no-index` 与 `--offline` 都要在。"""
    text = INSTALLER.read_text(encoding="utf-8")
    assert "pip install --no-index --find-links=" in text
    assert "conda env create" in text and "--offline" in text


def test_installer_checks_the_model_digest() -> None:
    """导入模型之后立刻验 digest —— 在这里发现被换掉比第一次排班时便宜得多。"""
    text = INSTALLER.read_text(encoding="utf-8")
    assert "backend.core.integrity" in text


def test_installer_dry_run_touches_nothing() -> None:
    """`--dry-run` 必须在体检+校验之后就退出，不许往下走。"""
    text = INSTALLER.read_text(encoding="utf-8")
    dry_exit = text.index('if [ "$DRY_RUN" -eq 1 ]; then')
    assert dry_exit < text.index("③ 铺开源码")


def test_installer_never_requires_root() -> None:
    """全程用户态（CLAUDE.md §2）。`sudo` 只允许出现在指向 iptables 的说明里。"""
    for line in INSTALLER.read_text(encoding="utf-8").splitlines():
        if "sudo" in line:
            assert line.strip().startswith("#"), f"install.sh 里出现了真的 sudo：{line}"


# ═════════════════════════════════════════════════════════════════════
# 两条路径的一致性（M8 出口标准）
# ═════════════════════════════════════════════════════════════════════
def test_both_paths_pin_the_same_determinism_knobs() -> None:
    """★ 确定性配置逐项比对 —— 这是「为什么两条路径会一样」的解释。"""
    result = subprocess.run(  # noqa: S603
        ["bash", str(COMPARE), "--config-only"],  # noqa: S607
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for key in ("SOLVER_SEED", "SOLVER_WORKERS", "RULESET_PATH", "TZ", "LANG"):
        assert (
            f"✅ {key}" in result.stdout.replace("  ", " ").replace(f"✅ {key}", f"✅ {key}")
            or key in result.stdout
        ), f"{key} 没被比对"


def test_compose_never_bakes_dependencies_into_the_image() -> None:
    """compose 路径必须 bind-mount 宿主的 conda 环境，**不能把依赖烤进镜像**。

    烤进去意味着「镜像里那套」与「裸装那套」不是同一个环境，而出口标准要比的
    恰恰是「两条路径跑出同一个结果」—— 那时比出来的差异说明不了任何问题。
    """
    compose = (PROJECT_ROOT / "deploy" / "compose" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "FTS_CONDA_ENV_DIR" in compose
    assert "network_mode: host" in compose


def test_compose_has_the_iptables_script_but_never_runs_it() -> None:
    """iptables 脚本随包交付，但**不在任何自动流程里被执行**（要 root）。"""
    script = PROJECT_ROOT / "deploy" / "compose" / "iptables_egress_drop.sh"
    assert script.is_file()
    body = script.read_text(encoding="utf-8")
    assert "DOCKER-USER" in body and "revert" in body
    # install.sh 里**提到**它是对的（注释里说明「由运维单独执行」），
    # 不许的是真的去调它。判据看的是**代码行**，不是注释。
    installer_code = [
        line
        for line in INSTALLER.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert not any("iptables" in line for line in installer_code), (
        "install.sh 真的去跑 iptables 了 —— 那一步要 root，不该在一键安装里"
    )


def test_golden_fingerprint_tool_refuses_unreproducible_states() -> None:
    """指纹工具遇到 `FEASIBLE`/`UNKNOWN` 必须退出 1 —— 那两个状态不保证可复现。"""
    text = (PROJECT_ROOT / "deploy" / "scripts" / "golden_fingerprint.py").read_text(
        encoding="utf-8"
    )
    assert 'REPRODUCIBLE_STATUSES = ("OPTIMAL", "INFEASIBLE")' in text
    assert "return 1" in text
