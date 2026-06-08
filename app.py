"""
OpenFOAM Knowledge Platform — app.py
現行バージョン: v1.0（AI Code QA 検証チェックリストまで搭載）

直近の変更点 (v0.3): リアルタイム残差監視 & 流場スライス画像プレビュー

変更点 (v0.2 → v0.3):
  ■ セクション1b: 残差パーサ
    - parse_residuals_from_log_line() : ログ1行から残差を正規表現で抽出
    - update_residual_dataframe()     : 抽出値を session_state の DataFrame に蓄積

  ■ セクション1c: 流場プレビュー
    - find_latest_surface_dir()       : postProcessing/surfaces/ 最新タイムステップを検出
    - load_vtk_surface_scalar()       : VTK PolyData から座標+スカラー値を numpy で軽量読み込み
    - render_surface_preview()        : matplotlib で 2D カラーマップを生成し temp_preview.png に上書き保存
    - try_update_preview()            : 更新間隔チェック付きのプレビュー更新トリガー

  ■ セクション1d: stream_wsl_run() を拡張 (v0.2 から変更)
    - ログ行ごとに残差を逐次パース → session_state.residual_df に蓄積
    - preview_interval ステップごとに render_surface_preview() を呼び出し

  ■ 画面2 (page_run_monitor):
    - モニタリングダッシュボードの残差タブを実データ表示に切り替え（対数軸）
    - ナレッジパネル下部に流場プレビューパネルを追加

NOTE:
  - 機密データ・固有パラメータは config.json / knowledge.json のみで管理。
  - このファイル自体には研究室固有の情報をハードコードしない。
  - VTK 読み込みは vtk / pyvista を optional import し、
    未インストール時は matplotlib の scatter fallback で動作する。

■ controlDict への functionObjects 設定例 (surfaces & residuals):
  詳細は knowledge.json の "controldict_hint" キー、または下記コメントを参照。

  functions
  {
      // --- 残差の CSV 書き出し ---
      residuals
      {
          type            residuals;
          libs            (utilityFunctionObjects);
          writeControl    timeStep;
          writeInterval   1;
          fields          (U p k omega);
      }

      // --- 中央断面スライスの VTK 書き出し ---
      surfaces
      {
          type            surfaces;
          libs            (sampling);
          writeControl    timeStep;
          writeInterval   50;          // プレビュー更新間隔に合わせる
          surfaceFormat   vtk;
          fields          (U p);

          surfaces
          (
              midPlane
              {
                  type        cuttingPlane;
                  planeType   pointAndNormal;
                  pointAndNormalDict
                  {
                      point  (0 0 0);
                      normal (0 0 1);
                  }
                  interpolate true;
              }
          );
      }
  }
"""

import io
import json
import math
import re
import random
import subprocess
import time
from pathlib import Path, PureWindowsPath
from typing import Optional

import matplotlib
matplotlib.use("Agg")   # GUI スレッド不要・スレッドセーフなバックエンドを強制
import matplotlib.pyplot as plt
import matplotlib.tri as mtri
import numpy as np
import pandas as pd
import streamlit as st

# optional: pyvista / vtk は未インストール環境でも起動できるよう遅延インポート
try:
    import pyvista as pv
    _PYVISTA_AVAILABLE = True
except ImportError:
    _PYVISTA_AVAILABLE = False

# ──────────────────────────────────────────────
# 0. 定数・パス設定
# ──────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
CONFIG_PATH   = BASE_DIR / "config.json"
KNOWLEDGE_PATH = BASE_DIR / "knowledge.json"

# 流場プレビュー画像の一時保存先（1ファイルを上書きし続けることでメモリリークを防ぐ）
PREVIEW_PNG   = BASE_DIR / "temp_preview.png"

PAGE_SETUP   = "⚙️  Setup"
PAGE_RUN     = "🚀  Run & Monitor"
PAGE_TROUBLE = "🔍  Troubleshoot"
PAGE_QA      = "✅  AI Code QA"

# ──────────────────────────────────────────────
# 1a. WSL コマンド実行バックエンド  [v0.2 から継続]
# ──────────────────────────────────────────────

_LOG_TAIL_LINES = 200   # ストリーミング表示の末尾バッファ行数


def win_to_wsl_path(win_path: str) -> str:
    """
    Windows の絶対パスを WSL マウントパスに変換する。
    例: "C:\\Users\\foo\\case"  ->  "/mnt/c/Users/foo/case"
    ネットワークパス (\\\\server\\...) は非対応。
    """
    p = PureWindowsPath(win_path)
    if not p.is_absolute():
        return win_path.replace("\\", "/")
    drive = p.drive.rstrip(":").lower()
    rest  = "/".join(p.parts[1:])
    return f"/mnt/{drive}/{rest}"


def _build_wsl_cmd(command: list[str], wsl_cwd: Optional[str]) -> list[str]:
    """WSL 呼び出し用コマンドリストを組み立てる。"""
    if wsl_cwd:
        inner = " ".join(command)
        return ["wsl", "bash", "-c", f"cd '{wsl_cwd}' && {inner}"]
    return ["wsl"] + command


def wsl_run(
    command: list[str],
    win_cwd: Optional[str] = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """
    短時間コマンド（blockMesh, checkMesh, decomposePar など）を WSL 経由で同期実行。

    Raises:
        subprocess.CalledProcessError : 非ゼロ終了（check=True）
        subprocess.TimeoutExpired     : タイムアウト
        FileNotFoundError             : wsl.exe 未検出
    """
    wsl_cwd = win_to_wsl_path(win_cwd) if win_cwd else None
    cmd = _build_wsl_cmd(command, wsl_cwd)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=True,
    )


def _save_log(label: str, text: str) -> None:
    """直近コマンドログを session_state に保存（Troubleshoot 画面連携用）。"""
    if "command_logs" not in st.session_state:
        st.session_state.command_logs = {}
    st.session_state.command_logs[label] = text


# ──────────────────────────────────────────────
# 1b. 残差パーサ  [v0.3 新規]
# ──────────────────────────────────────────────

# OpenFOAM 標準ログの残差行パターン
# 例: "smoothSolver:  Solving for Ux, Initial residual = 1.234e-04, ..."
# 例: "GAMG:  Solving for p, Initial residual = 5.678e-03, ..."
_RE_RESIDUAL = re.compile(
    r"Solving for (\w+),\s+Initial residual = ([0-9]+\.?[0-9]*(?:[eE][+-]?[0-9]+)?)"
)

# タイムステップ行パターン: "Time = 0.05"
_RE_TIME = re.compile(r"^Time\s*=\s*([0-9]+\.?[0-9]*(?:[eE][+-]?[0-9]+)?)")


def parse_residuals_from_log_line(line: str) -> Optional[tuple[str, float]]:
    """
    ログの1行を受け取り、残差情報 (変数名, Initial residual) を返す。
    残差行でない場合は None を返す。

    Args:
        line: ログの1行文字列

    Returns:
        ("Ux", 1.23e-4) のような (field_name, residual_value) タプル、
        または None
    """
    m = _RE_RESIDUAL.search(line)
    if m:
        field = m.group(1)
        value = float(m.group(2))
        return field, value
    return None


def parse_time_from_log_line(line: str) -> Optional[float]:
    """ログ1行からタイムステップ値を抽出する。"""
    m = _RE_TIME.match(line.strip())
    if m:
        return float(m.group(1))
    return None


def update_residual_dataframe(field: str, value: float) -> None:
    """
    抽出した残差値を session_state.residual_df に追記する。
    DataFrameは {field: [val, ...]} 形式で、行インデックスは抽出ステップ数。

    設計メモ:
      - 全ステップを保持するとメモリが膨らむため、
        _RESIDUAL_MAX_ROWS を超えた場合は先頭行を削除する（FIFO）。
    """
    _RESIDUAL_MAX_ROWS = 2000   # 保持する最大行数（メモリ上限）

    if "residual_df" not in st.session_state or st.session_state.residual_df is None:
        st.session_state.residual_df = pd.DataFrame()
    if "residual_step" not in st.session_state:
        st.session_state.residual_step = 0

    df = st.session_state.residual_df
    step = st.session_state.residual_step

    # 新規行を1行追加（存在しない列は NaN で埋まる）
    new_row = pd.DataFrame({field: [value]}, index=[step])
    if df.empty:
        df = new_row
    else:
        # 同一ステップに複数フィールドの残差が来る場合は同行に書き込む
        if step in df.index:
            df.loc[step, field] = value
        else:
            df = pd.concat([df, new_row])

    # FIFO: 上限を超えたら古い行を削除
    if len(df) > _RESIDUAL_MAX_ROWS:
        df = df.iloc[-_RESIDUAL_MAX_ROWS:]

    st.session_state.residual_df = df


def advance_residual_step() -> None:
    """タイムステップ行を検出したときに呼び出し、行カウンタを進める。"""
    st.session_state.residual_step = st.session_state.get("residual_step", 0) + 1


def reset_residual_dataframe() -> None:
    """新規 pimpleFoam 実行前に残差バッファをリセットする。"""
    st.session_state.residual_df   = pd.DataFrame()
    st.session_state.residual_step = 0


# ──────────────────────────────────────────────
# 1c. 流場プレビューレンダラ  [v0.3 新規]
# ──────────────────────────────────────────────

def find_latest_surface_dir(case_dir: str, surface_name: str = "midPlane") -> Optional[Path]:
    """
    postProcessing/surfaces/<最新タイムステップ>/<surface_name>.vtp (または .vtk) を探す。

    OpenFOAM の surfaces functionObject は以下の構造でファイルを生成する:
      <case>/postProcessing/surfaces/<time>/<surfaceName>_<field>.vtp

    Args:
        case_dir     : Windows 側のケースフォルダパス
        surface_name : controlDict で定義したサーフェス名（例: "midPlane"）

    Returns:
        見つかった VTK/VTP ファイルの Path、見つからない場合は None
    """
    surfaces_root = Path(case_dir) / "postProcessing" / "surfaces"
    if not surfaces_root.exists():
        return None

    # タイムステップフォルダを数値順でソートし最新を取得
    time_dirs = sorted(
        [d for d in surfaces_root.iterdir() if d.is_dir()],
        key=lambda d: float(d.name) if d.name.replace(".", "").isdigit() else -1,
    )
    if not time_dirs:
        return None

    latest_dir = time_dirs[-1]

    # surface_name を含む VTP / VTK ファイルを検索（フィールド名付きも許容）
    for ext in ("*.vtp", "*.vtk"):
        candidates = sorted(latest_dir.glob(ext))
        # surface_name にマッチするものを優先
        named = [f for f in candidates if surface_name in f.stem]
        if named:
            return named[0]
        if candidates:
            return candidates[0]   # fallback: 先頭ファイル

    return None


def load_vtk_surface_scalar(
    vtk_path: Path,
    field: str = "p",
) -> Optional[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """
    VTK PolyData ファイルから (x, y, scalar_values, connectivity) を numpy で読み込む。
    pyvista が利用可能な場合はそちらを使い、なければ手書きパーサで読む。

    設計メモ（軽量化のポイント）:
      - mesh.points のみ取得し、セル全体のトポロジーは使わない
      - スカラー配列は 1 フィールドのみ取得
      - 全頂点を読み込むが、レンダリング解像度は matplotlib 側で制御

    Args:
        vtk_path : VTK または VTP ファイルのパス
        field    : 可視化するスカラーフィールド名 (例: "p", "U_0" など)

    Returns:
        (x, y, z, scalars) の numpy 配列タプル、失敗時は None
    """
    if _PYVISTA_AVAILABLE:
        try:
            mesh = pv.read(str(vtk_path))
            pts = mesh.points
            x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]

            # フィールドが見つからない場合は利用可能な最初のスカラーを使う
            if field in mesh.point_data:
                scalars = np.asarray(mesh.point_data[field])
            elif mesh.point_data.keys():
                first_key = list(mesh.point_data.keys())[0]
                scalars = np.asarray(mesh.point_data[first_key])
                field = first_key
            else:
                return None

            # ベクトル場の場合は大きさ (magnitude) に変換
            if scalars.ndim == 2:
                scalars = np.linalg.norm(scalars, axis=1)

            return x, y, z, scalars

        except Exception:
            return None

    # ── pyvista 未インストール時のフォールバック: ASCII VTK レガシー形式のみ対応 ──
    try:
        return _parse_legacy_vtk_ascii(vtk_path, field)
    except Exception:
        return None


def _parse_legacy_vtk_ascii(
    vtk_path: Path,
    field: str,
) -> Optional[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """
    ASCII 形式の VTK レガシーファイル (.vtk) を最小限のパーサで読み込む。
    POINTS セクションと SCALARS / VECTORS セクションのみを解析する。
    VTP (XML 形式) には非対応。
    """
    text = vtk_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    points: list[list[float]] = []
    scalars: list[float] = []

    i = 0
    n_points = 0
    reading_points  = False
    reading_scalars = False
    reading_vectors = False

    while i < len(lines):
        ln = lines[i].strip()

        if ln.upper().startswith("POINTS"):
            parts = ln.split()
            n_points = int(parts[1])
            reading_points = True
            i += 1
            continue

        if reading_points:
            for tok in ln.split():
                points.append(float(tok))
            if len(points) >= n_points * 3:
                reading_points = False
            i += 1
            continue

        if ln.upper().startswith("SCALARS"):
            reading_scalars = True
            reading_vectors = False
            i += 2   # "LOOKUP_TABLE default" 行をスキップ
            continue

        if ln.upper().startswith("VECTORS"):
            reading_vectors = True
            reading_scalars = False
            i += 1
            continue

        if reading_scalars or reading_vectors:
            for tok in ln.split():
                scalars.append(float(tok))

        i += 1

    if not points:
        return None

    pts = np.array(points).reshape(-1, 3)
    x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]

    if not scalars:
        scalars_arr = np.zeros(len(x))
    else:
        raw = np.array(scalars)
        if reading_vectors:
            # ベクトルを magnitude に変換
            raw = raw[: len(x) * 3].reshape(-1, 3)
            scalars_arr = np.linalg.norm(raw, axis=1)
        else:
            scalars_arr = raw[: len(x)]

    return x, y, z, scalars_arr


def render_surface_preview(
    vtk_path: Path,
    field: str = "p",
    cmap: str = "RdBu_r",
    dpi: int = 120,
    output_path: Optional[Path] = None,
) -> bool:
    """
    VTK サーフェスデータを matplotlib の 2D カラーマップとして描画し、
    PREVIEW_PNG (または output_path) に上書き保存する。

    軽量化ポイント:
      - figsize=(7, 4), dpi=120 の低解像度レンダリング
      - plt.close(fig) で即座にメモリ解放
      - ファイルは1つを上書き保存（メモリリーク防止）
      - 3D 描画・インタラクティブ表示は行わない

    Args:
        vtk_path    : 入力 VTK/VTP ファイルパス
        field       : スカラーフィールド名
        cmap        : Matplotlib カラーマップ名
        dpi         : 出力解像度
        output_path : 保存先パス（None の場合は PREVIEW_PNG を使用）

    Returns:
        True: 正常保存, False: 失敗
    """
    out = output_path or PREVIEW_PNG
    result = load_vtk_surface_scalar(vtk_path, field)
    if result is None:
        return False

    x, y, z, scalars = result

    # 断面の主軸を選択（z 方向が一定なら XY 平面、y 方向が一定なら XZ 平面）
    z_range = np.ptp(z)
    y_range = np.ptp(y)
    if z_range < y_range * 0.01:
        hx, hy, axis_labels = x, y, ("x [m]", "y [m]")
    else:
        hx, hy, axis_labels = x, z, ("x [m]", "z [m]")

    fig, ax = plt.subplots(figsize=(7, 4), dpi=dpi)
    fig.patch.set_facecolor("#0e1117")   # Streamlit ダークテーマに合わせた背景色
    ax.set_facecolor("#0e1117")

    try:
        # 三角形メッシュ補間で滑らかに描画（点数が少ない場合は scatter にフォールバック）
        if len(hx) >= 9:
            triang = mtri.Triangulation(hx, hy)
            tc = ax.tripcolor(triang, scalars, cmap=cmap, shading="gouraud")
        else:
            tc = ax.scatter(hx, hy, c=scalars, cmap=cmap, s=4)
    except Exception:
        tc = ax.scatter(hx, hy, c=scalars, cmap=cmap, s=4)

    cbar = fig.colorbar(tc, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label(field, color="white", fontsize=9)
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color="white")

    ax.set_xlabel(axis_labels[0], color="white", fontsize=9)
    ax.set_ylabel(axis_labels[1], color="white", fontsize=9)
    ax.tick_params(colors="white", labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor("#444444")

    time_label = vtk_path.parent.name   # タイムステップ番号をタイトルに
    ax.set_title(
        f"Flow Preview  |  field: {field}  |  t = {time_label}",
        color="white", fontsize=10,
    )

    fig.tight_layout()
    fig.savefig(str(out), bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)   # 重要: 毎回 close してメモリ解放
    return True


def try_update_preview(
    case_dir: str,
    field: str,
    surface_name: str,
    cmap: str,
    step_counter: int,
    update_every: int,
) -> bool:
    """
    step_counter が update_every の倍数の場合にのみプレビューを更新する。
    高頻度更新によるレンダリング負荷を抑える間引き制御。

    Args:
        case_dir      : Windows 側ケースフォルダ
        field         : 描画フィールド名
        surface_name  : surfaces functionObject のサーフェス名
        cmap          : カラーマップ名
        step_counter  : 現在のタイムステップカウンタ
        update_every  : 何ステップごとに更新するか

    Returns:
        True: 更新を実行, False: スキップまたは失敗
    """
    if update_every <= 0 or (step_counter % update_every) != 0:
        return False

    vtk_path = find_latest_surface_dir(case_dir, surface_name)
    if vtk_path is None:
        return False

    return render_surface_preview(vtk_path, field=field, cmap=cmap)


# ──────────────────────────────────────────────
# 1d. stream_wsl_run (v0.3 拡張版)
#     残差パース & プレビュー更新をストリーミングループに統合
# ──────────────────────────────────────────────

def stream_wsl_run(
    command: list[str],
    win_cwd: Optional[str],
    log_placeholder: "st.delta_generator.DeltaGenerator",
    residual_placeholder: Optional["st.delta_generator.DeltaGenerator"] = None,
    preview_placeholder: Optional["st.delta_generator.DeltaGenerator"] = None,
    preview_cfg: Optional[dict] = None,
    timeout: int = 3600,
) -> tuple[int, str]:
    """
    長時間プロセス（pimpleFoam など）を WSL 経由でストリーミング実行する。
    [v0.3 拡張] ログ行ごとに残差パース・グラフ更新・プレビュー更新を実行。

    Args:
        command              : WSL 内コマンドリスト
        win_cwd              : Windows 側作業ディレクトリ
        log_placeholder      : ログ表示用 st.empty()
        residual_placeholder : 残差グラフ用 st.empty()（None の場合はスキップ）
        preview_placeholder  : 流場プレビュー用 st.empty()（None の場合はスキップ）
        preview_cfg          : プレビュー設定 dict
                               {"field": "p", "surface_name": "midPlane",
                                "cmap": "RdBu_r", "update_every": 50}
        timeout              : タイムアウト秒数

    Returns:
        (returncode, full_log_text) のタプル
    """
    wsl_cwd = win_to_wsl_path(win_cwd) if win_cwd else None
    cmd = _build_wsl_cmd(command, wsl_cwd)

    lines: list[str] = []
    start = time.time()
    step_counter = 0
    cfg = preview_cfg or {}

    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as proc:
        for raw_line in proc.stdout:
            # ── タイムアウト監視 ──
            if time.time() - start > timeout:
                proc.kill()
                log_placeholder.error("⏰ タイムアウト: プロセスを強制終了しました。")
                return -1, "\n".join(lines)

            line = raw_line.rstrip()
            lines.append(line)

            # ── ログ表示（末尾 N 行） ──
            tail = lines[-_LOG_TAIL_LINES:]
            log_placeholder.code("\n".join(tail), language="bash")

            # ── タイムステップ検出 → ステップカウンタを進める ──
            if parse_time_from_log_line(line) is not None:
                advance_residual_step()
                step_counter += 1

            # ── 残差パース & DataFrame 蓄積 ──
            parsed = parse_residuals_from_log_line(line)
            if parsed is not None:
                field_name, residual_val = parsed
                update_residual_dataframe(field_name, residual_val)

                # 残差グラフをリアルタイム更新
                if residual_placeholder is not None:
                    df = st.session_state.get("residual_df")
                    if df is not None and not df.empty:
                        _render_residual_chart(residual_placeholder, df)

            # ── 流場プレビュー更新（間引き制御） ──
            if preview_placeholder is not None and win_cwd and cfg:
                updated = try_update_preview(
                    case_dir     = win_cwd,
                    field        = cfg.get("field", "p"),
                    surface_name = cfg.get("surface_name", "midPlane"),
                    cmap         = cfg.get("cmap", "RdBu_r"),
                    step_counter = step_counter,
                    update_every = cfg.get("update_every", 50),
                )
                if updated and PREVIEW_PNG.exists():
                    preview_placeholder.image(
                        str(PREVIEW_PNG),
                        caption=f"Flow Preview (t-step: {step_counter})",
                        use_container_width=True,
                    )

        proc.wait()
        return proc.returncode, "\n".join(lines)


def _render_residual_chart(
    placeholder: "st.delta_generator.DeltaGenerator",
    df: pd.DataFrame,
) -> None:
    """
    残差 DataFrame を matplotlib で対数軸グラフとして描画し、
    st.empty() プレースホルダーにバイト列として書き込む。

    st.line_chart では対数軸が使えないため matplotlib を使用。
    plt.close() でメモリを即解放する。
    """
    if df.empty:
        return

    fig, ax = plt.subplots(figsize=(6, 3), dpi=100)
    fig.patch.set_facecolor("#0e1117")
    ax.set_facecolor("#1a1d24")

    colors = plt.cm.tab10.colors
    for idx, col in enumerate(df.columns):
        series = df[col].dropna()
        if series.empty:
            continue
        # 非正値は対数軸で扱えないためクリップ
        series = series.clip(lower=1e-16)
        ax.semilogy(
            series.index,
            series.values,
            label=col,
            color=colors[idx % len(colors)],
            linewidth=1.2,
        )

    ax.set_xlabel("Iteration step", color="white", fontsize=8)
    ax.set_ylabel("Initial Residual [-]", color="white", fontsize=8)
    ax.tick_params(colors="white", labelsize=7)
    ax.yaxis.grid(True, which="both", color="#333333", linewidth=0.5)
    ax.xaxis.grid(True, color="#333333", linewidth=0.5)
    for spine in ax.spines.values():
        spine.set_edgecolor("#444444")
    leg = ax.legend(fontsize=7, facecolor="#1a1d24", labelcolor="white",
                    loc="upper right", framealpha=0.7)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    placeholder.image(buf, use_container_width=True)


# ──────────────────────────────────────────────
# 1e. run_command_with_ui (v0.3: streaming 引数に v0.3 プレースホルダーを追加)
# ──────────────────────────────────────────────

def run_command_with_ui(
    label: str,
    command: list[str],
    win_cwd: Optional[str],
    streaming: bool = False,
    timeout: int = 120,
    residual_placeholder: Optional["st.delta_generator.DeltaGenerator"] = None,
    preview_placeholder: Optional["st.delta_generator.DeltaGenerator"] = None,
    preview_cfg: Optional[dict] = None,
) -> None:
    """
    コマンドを実行し結果を Streamlit UI に表示する共通ハンドラ。
    [v0.3 拡張] streaming 時に残差グラフ・流場プレビューのプレースホルダーを受け取る。
    """
    if not win_cwd:
        st.error(
            "⚠️ ケースフォルダが設定されていません。\n\n"
            "「作業ディレクトリ」欄にケースフォルダの Windows パスを入力してください。"
        )
        return

    if streaming:
        st.info(f"▶️ **{label}** を実行中... （完了まで画面を閉じないでください）")
        log_placeholder = st.empty()

        # pimpleFoam 開始時に残差バッファをリセット
        if label == "pimpleFoam":
            reset_residual_dataframe()

        returncode, full_log = stream_wsl_run(
            command=command,
            win_cwd=win_cwd,
            log_placeholder=log_placeholder,
            residual_placeholder=residual_placeholder,
            preview_placeholder=preview_placeholder,
            preview_cfg=preview_cfg,
            timeout=timeout,
        )
        _save_log(label, full_log)

        if returncode == 0:
            st.success(f"✅ **{label}** が正常に完了しました。（終了コード: 0）")
        else:
            st.error(
                f"❌ **{label}** がエラーで終了しました（終了コード: {returncode}）。\n\n"
                "Troubleshoot 画面でエラー内容を解析してください。"
            )
    else:
        with st.spinner(f"⏳ {label} を実行中..."):
            try:
                result = wsl_run(command=command, win_cwd=win_cwd, timeout=timeout)
                output = result.stdout + result.stderr
                _save_log(label, output)
                st.success(f"✅ **{label}** が正常に完了しました。（終了コード: 0）")
                if output.strip():
                    with st.expander("📋 実行ログ", expanded=False):
                        st.code(output, language="bash")

            except subprocess.CalledProcessError as e:
                error_output = (e.stdout or "") + (e.stderr or "")
                _save_log(label, error_output)
                st.error(
                    f"❌ **{label}** がエラーで終了しました（終了コード: {e.returncode}）。\n\n"
                    "Troubleshoot 画面にログを貼り付けて原因を解析してください。"
                )
                with st.expander("🔴 エラーログ（詳細）", expanded=True):
                    st.code(error_output or "（出力なし）", language="bash")

            except subprocess.TimeoutExpired:
                st.error(
                    f"⏰ **{label}** がタイムアウト（{timeout}秒）で強制停止されました。\n\n"
                    "ターミナルから直接実行して状況を確認してください。"
                )

            except FileNotFoundError:
                st.error(
                    "❌ `wsl.exe` が見つかりません。\n\n"
                    "WSL がインストールされているか、PATH が正しく設定されているか確認してください。"
                )


# ──────────────────────────────────────────────
# 2. データ読み込みユーティリティ
# ──────────────────────────────────────────────

@st.cache_data
def load_config() -> dict:
    """config.json を読み込む。ファイルが存在しない場合は空辞書を返す。"""
    if not CONFIG_PATH.exists():
        st.error(f"設定ファイルが見つかりません: {CONFIG_PATH}")
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


@st.cache_data
def load_knowledge() -> dict:
    """knowledge.json を読み込む。ファイルが存在しない場合は空辞書を返す。"""
    if not KNOWLEDGE_PATH.exists():
        st.error(f"ナレッジファイルが見つかりません: {KNOWLEDGE_PATH}")
        return {}
    with open(KNOWLEDGE_PATH, encoding="utf-8") as f:
        return json.load(f)


# ──────────────────────────────────────────────
# 3. ダミーデータ生成ユーティリティ（残差グラフ未使用時の初期表示用）
# ──────────────────────────────────────────────

def generate_dummy_residuals(steps: int = 80) -> pd.DataFrame:
    """
    残差の推移を模したダミー時系列データを生成する。
    pimpleFoam 未実行時のグラフ初期表示に使用する。
    """
    knowledge = load_knowledge()
    fields = knowledge.get("monitor", {}).get("residual_fields", ["Ux", "p"])
    data = {}
    for field in fields:
        base = 1e-1
        decay = [base * math.exp(-0.05 * t) * (1 + 0.1 * random.gauss(0, 1)) for t in range(steps)]
        data[field] = [max(v, 1e-12) for v in decay]
    return pd.DataFrame(data)


def generate_dummy_forces(steps: int = 80) -> pd.DataFrame:
    """揚力・抗力・揚抗比のダミー時系列データを生成する。"""
    lift     = [1.2 + 0.05 * math.sin(0.3 * t) + 0.01 * random.gauss(0, 1) for t in range(steps)]
    drag     = [0.15 + 0.005 * math.cos(0.2 * t) + 0.002 * random.gauss(0, 1) for t in range(steps)]
    ld_ratio = [l / d for l, d in zip(lift, drag)]
    return pd.DataFrame({"lift": lift, "drag": drag, "lift_drag_ratio": ld_ratio})


# ──────────────────────────────────────────────
# 4. 画面1: Setup
# ──────────────────────────────────────────────

def page_setup(config: dict) -> None:
    """ケース設定・テンプレート生成画面"""
    st.header("⚙️ Case Setup")
    st.caption("外部 config.json から読み込んだデフォルト値を初期値として使用しています。")

    defaults = config.get("defaults", {})

    st.subheader("📐 基本パラメータ")
    col1, col2, col3 = st.columns(3)

    with col1:
        re = st.number_input(
            "レイノルズ数 Re [-]",
            min_value=1, max_value=10_000_000,
            value=int(defaults.get("reynolds_number", 100000)),
            step=1000,
            help="流れの慣性力と粘性力の比。Re = U * L / ν",
        )
        u_ref = st.number_input(
            "代表速度 U_ref [m/s]",
            min_value=0.01, max_value=500.0,
            value=float(defaults.get("reference_velocity", 10.0)),
            step=0.5,
        )
    with col2:
        l_ref = st.number_input(
            "代表長さ L_ref [m]",
            min_value=0.001, max_value=100.0,
            value=float(defaults.get("reference_length", 1.0)),
            step=0.01,
        )
        nu = st.number_input(
            "動粘性係数 ν [m²/s]",
            min_value=1e-8, max_value=1e-2,
            value=float(defaults.get("kinematic_viscosity", 1e-5)),
            format="%.2e",
        )
    with col3:
        aoa = st.number_input(
            "迎角 AoA [deg]",
            min_value=-30.0, max_value=30.0,
            value=float(defaults.get("angle_of_attack", 5.0)),
            step=0.5,
        )
        rho = st.number_input(
            "空気密度 ρ [kg/m³]",
            min_value=0.1, max_value=2.0,
            value=float(defaults.get("air_density", 1.225)),
            step=0.001, format="%.3f",
        )

    calculated_re = u_ref * l_ref / nu if nu > 0 else 0
    st.info(f"🧮 入力値から計算された Re = **{calculated_re:,.0f}**  （参考値: config.json の Re = {re:,}）")

    st.divider()
    st.subheader("⏱️ タイムステップ & 出力設定")
    col4, col5, col6 = st.columns(3)
    with col4:
        end_time = st.number_input(
            "終了時刻 endTime [s]",
            min_value=0.001,
            value=float(defaults.get("end_time", 5.0)),
            step=0.5,
        )
    with col5:
        delta_t = st.number_input(
            "タイムステップ ΔT [s]",
            min_value=1e-6, max_value=1.0,
            value=float(defaults.get("delta_t", 0.001)),
            format="%.4f",
        )
    with col6:
        write_interval = st.number_input(
            "書き出し間隔 writeInterval [-]",
            min_value=1, max_value=10000,
            value=int(defaults.get("write_interval", 50)),
        )

    st.divider()
    st.subheader("🌀 乱流モデル & 並列化")
    col7, col8 = st.columns(2)
    with col7:
        turb_models  = config.get("turbulence_models", ["kOmegaSST"])
        default_turb = defaults.get("turbulence_model", "kOmegaSST")
        default_idx  = turb_models.index(default_turb) if default_turb in turb_models else 0
        turb_model = st.selectbox("乱流モデル", turb_models, index=default_idx)
    with col8:
        n_sub = st.number_input(
            "並列コア数 (numberOfSubdomains)",
            min_value=1, max_value=256,
            value=int(defaults.get("num_subdomains", 4)),
        )
        decomp_methods = config.get("decompose_methods", ["scotch"])
        decomp_method  = st.selectbox("分割手法", decomp_methods)

    st.divider()
    st.subheader("📁 ケースフォルダ生成")
    col_name, col_path = st.columns([1, 2])
    with col_name:
        case_name = st.text_input(
            "ケース名", value="myAirfoilCase",
            help="生成されるフォルダ名になります。",
        )
    with col_path:
        default_path   = st.session_state.get("case_dir", r"C:\OpenFOAM\cases")
        case_dir_input = st.text_input(
            "作業ディレクトリ（Windows パス）",
            value=default_path,
            help="OpenFOAM ケースフォルダの Windows 絶対パス。Run 画面でも使用されます。",
        )
    st.session_state.case_dir = case_dir_input

    if case_dir_input:
        st.caption(f"🐧 WSL マウントパス (自動変換): `{win_to_wsl_path(case_dir_input)}`")

    if st.button("📂 ケースフォルダを生成", type="primary", use_container_width=True):
        # TODO: v1.1 で実装
        #   1. templateDir からベースケースをコピー (shutil.copytree)
        #   2. Jinja2 テンプレートエンジンで blockMeshDict / controlDict を書き換え
        #   3. run_command_with_ui("blockMesh", ["blockMesh"], case_dir_input) を呼び出す
        st.success(
            f"✅ **[MOCK]** ケース「{case_name}」の生成パラメータを確認しました。\n\n"
            f"- 乱流モデル: `{turb_model}`\n"
            f"- 分割数: `{n_sub}` コア / 手法: `{decomp_method}`\n"
            f"- 終了時刻: `{end_time} s` / ΔT: `{delta_t} s`\n"
            f"- 作業ディレクトリ: `{case_dir_input}`\n\n"
            "⚠️ 現在のバージョンではファイルの実書き込みは未実装です。"
        )
        st.balloons()


# ──────────────────────────────────────────────
# 5. 画面2: Run & Monitor  [v0.3: 残差リアルタイム & 流場プレビュー統合]
# ──────────────────────────────────────────────

def page_run_monitor(knowledge: dict) -> None:
    """解析実行・ナレッジ表示・残差リアルタイム監視・流場プレビュー画面"""
    st.header("🚀 Run & Monitor")

    commands_knowledge = knowledge.get("commands", {})

    # ── 作業ディレクトリ & 並列設定バー ──
    with st.container(border=True):
        st.markdown("##### 📂 作業ディレクトリ & 実行設定")
        col_dir, col_mpi = st.columns([3, 1])
        with col_dir:
            case_dir = st.text_input(
                "Windows パス",
                value=st.session_state.get("case_dir", ""),
                label_visibility="collapsed",
                placeholder=r"例: C:\OpenFOAM\cases\myAirfoilCase",
            )
            st.session_state.case_dir = case_dir
            if case_dir:
                st.caption(f"🐧 WSL: `{win_to_wsl_path(case_dir)}`")
            else:
                st.caption("⚠️ パスが未設定です。Setup 画面で設定してください。")
        with col_mpi:
            n_cores = st.number_input(
                "並列コア数", min_value=1, max_value=256,
                value=st.session_state.get("n_cores", 4),
            )
            st.session_state.n_cores = n_cores

    if "active_command" not in st.session_state:
        st.session_state.active_command = None

    st.divider()

    # ── プレビュー設定（折りたたみ） ──
    with st.expander("🎨 流場プレビュー設定 (surfaces functionObject 連携)", expanded=False):
        st.caption(
            "controlDict の functions ブロックに surfaces functionObject を追加すると、\n"
            "`postProcessing/surfaces/<time>/<surfaceName>_<field>.vtp` が生成されます。\n"
            "下記パラメータは surfaces の設定と一致させてください。"
        )
        pcol1, pcol2, pcol3, pcol4 = st.columns(4)
        with pcol1:
            preview_field   = st.text_input("フィールド名", value="p",
                                             help="p, U, k, omega など")
        with pcol2:
            surface_name    = st.text_input("サーフェス名", value="midPlane",
                                             help="controlDict で定義したサーフェス名")
        with pcol3:
            preview_cmap    = st.selectbox(
                "カラーマップ", ["RdBu_r", "viridis", "plasma", "coolwarm", "jet"],
                help="matplotlib カラーマップ名",
            )
        with pcol4:
            preview_every   = st.number_input(
                "更新間隔 (タイムステップ数)", min_value=1, max_value=500, value=50,
                help="何タイムステップごとにプレビュー画像を更新するか。小さいほど負荷が高い。",
            )

        preview_cfg = {
            "field":        preview_field,
            "surface_name": surface_name,
            "cmap":         preview_cmap,
            "update_every": int(preview_every),
        }

        # pyvista の有無をユーザーに通知
        if _PYVISTA_AVAILABLE:
            st.success("✅ pyvista が利用可能です。高品質な VTK 読み込みが有効です。")
        else:
            st.warning(
                "⚠️ pyvista が未インストールのため、ASCII VTK レガシー形式のみ対応です。\n"
                "`pip install pyvista` でインストールすると VTP 形式も読み込めます。"
            )

    # ── コマンド定義テーブル ──
    COMMAND_DEFS: dict[str, dict] = {
        "blockMesh":      {"cmd": ["blockMesh"],      "streaming": False, "mpi": False, "timeout": 300},
        "checkMesh":      {"cmd": ["checkMesh"],      "streaming": False, "mpi": False, "timeout": 120},
        "decomposePar":   {"cmd": ["decomposePar"],   "streaming": False, "mpi": False, "timeout": 300},
        "pimpleFoam":     {"cmd": ["pimpleFoam"],     "streaming": True,  "mpi": True,  "timeout": 3600},
        "reconstructPar": {"cmd": ["reconstructPar", "-latestTime"], "streaming": False, "mpi": False, "timeout": 600},
        "paraFoam":       {"cmd": ["paraFoam", "&"],  "streaming": False, "mpi": False, "timeout": 30},
        # ── テストスクリプト ──
        # cmd は文字列ではなくリストで管理（_build_wsl_cmd が " ".join するため）
        "Run Test Script": {"cmd": ["python3", "script/run_test.py"], "streaming": True, "mpi": False, "timeout": 300},
    }

    # OpenFOAM コマンドと Test Script を分けて表示するためキーセットを分離
    _OF_CMDS   = ["blockMesh", "checkMesh", "decomposePar", "pimpleFoam", "reconstructPar", "paraFoam"]
    _TEST_CMDS = ["Run Test Script"]

    # ── 上部: コマンド実行 & ナレッジパネル ──
    left_col, right_col = st.columns([1, 2], gap="large")

    # ─ ストリーミング中に使うプレースホルダーをここで確保 ─
    # (Streamlit は列内で st.empty() を使うと列がつぶれるため、列の外側に置く)
    residual_ph_ref: list = []   # ミュータブルなリストでクロージャに渡す
    preview_ph_ref:  list = []

    with left_col:
        st.subheader("🖥️ コマンド実行パネル")
        st.caption("ボタンを押すと WSL 経由でコマンドが実行されます。")

        for cmd in _OF_CMDS:
            icon  = commands_knowledge.get(cmd, {}).get("icon", "▶️")

            if st.button(f"{icon} {cmd}", key=f"btn_{cmd}", use_container_width=True):
                st.session_state.active_command = cmd
                defn       = COMMAND_DEFS[cmd]
                actual_cmd = list(defn["cmd"])

                if defn.get("mpi") and n_cores > 1:
                    actual_cmd = ["mpirun", "-np", str(n_cores)] + defn["cmd"] + ["-parallel"]

                # streaming コマンドのプレースホルダーを列の外に生成
                res_ph  = residual_ph_ref[0] if residual_ph_ref else None
                prev_ph = preview_ph_ref[0]  if preview_ph_ref  else None

                run_command_with_ui(
                    label=cmd,
                    command=actual_cmd,
                    win_cwd=case_dir or None,
                    streaming=defn["streaming"],
                    timeout=defn["timeout"],
                    residual_placeholder=res_ph,
                    preview_placeholder=prev_ph,
                    preview_cfg=preview_cfg if defn["streaming"] else None,
                )

        # ── テストスクリプト専用セクション ──
        st.divider()
        st.markdown("**🧪 テストスクリプト**")
        st.caption("Python スクリプトを WSL 経由で実行します。")

        for cmd in _TEST_CMDS:
            defn = COMMAND_DEFS[cmd]

            if st.button(
                f"🧪 {cmd}",
                key=f"btn_{cmd}",
                use_container_width=True,
                type="secondary",
                help=f"実行コマンド: {' '.join(defn['cmd'])}",
            ):
                st.session_state.active_command = cmd
                res_ph  = residual_ph_ref[0] if residual_ph_ref else None
                prev_ph = preview_ph_ref[0]  if preview_ph_ref  else None

                run_command_with_ui(
                    label=cmd,
                    command=defn["cmd"],
                    win_cwd=case_dir or None,
                    streaming=defn["streaming"],
                    timeout=defn["timeout"],
                    residual_placeholder=res_ph,
                    preview_placeholder=prev_ph,
                    preview_cfg=None,   # テストスクリプトはプレビュー不要
                )

        st.divider()
        if st.session_state.get("command_logs"):
            log_text = "\n\n".join(
                f"=== {k} ===\n{v}"
                for k, v in st.session_state.command_logs.items()
            )
            st.download_button(
                label="📥 実行ログを保存",
                data=log_text,
                file_name="openfoam_run_log.txt",
                mime="text/plain",
                use_container_width=True,
            )

    with right_col:
        st.subheader("📖 ナレッジパネル")
        active = st.session_state.active_command

        if active is None:
            st.info("👈 左側のボタンを押すと、そのコマンドに関するノウハウが表示されます。")
        else:
            cmd_info     = commands_knowledge.get(active, {})
            title        = cmd_info.get("title", active)
            tips         = cmd_info.get("tips", [])
            warning_text = cmd_info.get("warning")

            st.markdown(f"### {title}")
            for i, tip in enumerate(tips, 1):
                st.info(f"💡 **Tip {i}:** {tip}")
            if warning_text:
                st.warning(f"⚠️ **注意:** {warning_text}")
            if not tips and not warning_text:
                st.info("knowledge.json にノウハウを追記してください。")

            logs = st.session_state.get("command_logs", {})
            if active in logs:
                st.divider()
                with st.expander(f"📋 {active} の直近ログ", expanded=False):
                    st.code(logs[active][-4000:], language="bash")

        # ── 流場プレビューパネル（ナレッジパネル下部） ──
        st.divider()
        st.markdown("##### 🖼️ 流場プレビュー")

        prev_ph = st.empty()
        preview_ph_ref.append(prev_ph)   # コマンドボタン側から参照できるようにリストに格納

        # 手動更新ボタン（計算完了後にも使える）
        col_btn, col_info = st.columns([1, 2])
        with col_btn:
            if st.button("🔄 プレビュー更新", use_container_width=True, key="manual_preview"):
                vtk_path = find_latest_surface_dir(case_dir, surface_name) if case_dir else None
                if vtk_path:
                    ok = render_surface_preview(vtk_path, field=preview_field, cmap=preview_cmap)
                    if ok and PREVIEW_PNG.exists():
                        prev_ph.image(str(PREVIEW_PNG), use_container_width=True,
                                      caption=f"Field: {preview_field}  |  Surface: {surface_name}")
                    else:
                        prev_ph.warning("⚠️ VTK 読み込みに失敗しました。フィールド名とサーフェス名を確認してください。")
                else:
                    prev_ph.info(
                        "📂 postProcessing/surfaces/ が見つかりません。\n\n"
                        "pimpleFoam 実行後、または controlDict に surfaces "
                        "functionObject が設定されているか確認してください。"
                    )
        with col_info:
            if PREVIEW_PNG.exists():
                mtime = PREVIEW_PNG.stat().st_mtime
                st.caption(f"最終更新: {time.strftime('%H:%M:%S', time.localtime(mtime))}")
            else:
                st.caption("プレビュー未生成")

        # 既存プレビュー画像があれば表示（前回実行のキャッシュ）
        if PREVIEW_PNG.exists() and not prev_ph._active:
            prev_ph.image(str(PREVIEW_PNG), use_container_width=True,
                          caption=f"Field: {preview_field}  |  Surface: {surface_name}（前回実行時）")

    st.divider()

    # ── モニタリングダッシュボード ──
    st.subheader("📊 モニタリングダッシュボード")

    tab_residual, tab_forces = st.tabs(["📉 残差 (Residuals)", "✈️ 揚抗力 (Forces)"])

    with tab_residual:
        # ストリーミング中は stream_wsl_run 内で _render_residual_chart が更新するので
        # ここでは「最終結果 or ダミー」を表示する静的ビューとして機能する。
        res_ph = st.empty()
        residual_ph_ref.append(res_ph)   # ストリーミング中に書き込めるよう登録

        real_df = st.session_state.get("residual_df")
        if real_df is not None and not real_df.empty:
            st.caption(f"✅ 実データを表示中（{len(real_df)} ステップ蓄積）")
            _render_residual_chart(res_ph, real_df)
        else:
            st.caption("⚠️ pimpleFoam 実行前はダミーデータを表示しています。")
            dummy_df = generate_dummy_residuals(steps=80)
            _render_residual_chart(res_ph, dummy_df)

        # 残差クリアボタン
        if st.button("🗑️ 残差データをクリア", key="clear_residuals"):
            reset_residual_dataframe()
            st.rerun()

    with tab_forces:
        # TODO: v1.1 で実装
        #   1. Path(case_dir) / "postProcessing" / "forces" / "0" / "force.dat" を読み込む
        #   2. 列: time  Fx  Fy  Fz  をパース（先頭 # 行をスキップ）
        #   3. 揚力 = Fy, 抗力 = Fx (迎角に応じて回転変換が必要な場合あり)
        #   4. L/D = lift / drag を計算して表示
        st.caption("⚠️ 現在はダミーデータを表示しています。実データ読み込みは v1.1 で実装予定。")
        df_forces = generate_dummy_forces(steps=80)
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            st.line_chart(df_forces[["lift", "drag"]], height=260)
        with col_f2:
            st.line_chart(df_forces[["lift_drag_ratio"]], height=260)


# ──────────────────────────────────────────────
# 6. 画面3: Troubleshoot
# ──────────────────────────────────────────────

def page_troubleshoot(knowledge: dict) -> None:
    """エラーログ解析・お悩み相談画面"""
    st.header("🔍 Troubleshoot")
    st.caption("ターミナルのエラーログを貼り付けると、知識ベースから原因と対処法を提案します。")

    troubleshoot_data  = knowledge.get("troubleshoot", {})
    patterns           = troubleshoot_data.get("patterns", [])
    default_response   = troubleshoot_data.get("default_response", {})

    saved_logs = st.session_state.get("command_logs", {})
    if saved_logs:
        st.markdown("**📋 Run 画面の実行ログから読み込む**")
        selected_log = st.selectbox(
            "貼り付けるログを選択",
            ["（手動入力）"] + list(saved_logs.keys()),
            label_visibility="collapsed",
        )
        paste_value = saved_logs.get(selected_log, "") if selected_log != "（手動入力）" else ""
    else:
        paste_value = ""

    error_log = st.text_area(
        "エラーログを貼り付けてください",
        value=paste_value,
        height=220,
        placeholder=(
            "例:\n"
            "--> FOAM FATAL ERROR:\n"
            "    Courant Number mean: 1.23  max: 5.67\n"
            "    ...\n\n"
            "log.pimpleFoam の末尾付近のメッセージを貼り付けると効果的です。"
        ),
    )

    if st.button("🔬 原因を解析", type="primary", use_container_width=True):
        if not error_log.strip():
            st.warning("ログが入力されていません。")
            return

        # TODO: v1.1 で実装
        #   Anthropic API / ローカル LLM によるより高度な自然言語ログ解析。
        matched = [
            p for p in patterns
            if any(kw.lower() in error_log.lower() for kw in p.get("keywords", []))
        ]

        st.divider()
        st.subheader("🧾 解析結果")

        if matched:
            for m in matched:
                sev = m.get("severity", "info")
                if sev == "error":
                    st.error(f"🔴 **{m.get('title', '')}**")
                elif sev == "warning":
                    st.warning(f"🟡 **{m.get('title', '')}**")
                else:
                    st.info(f"🔵 **{m.get('title', '')}**")
                with st.expander("詳細を見る", expanded=True):
                    st.markdown(f"**🔎 推定原因:**\n{m.get('cause', '')}")
                    st.markdown(f"**🛠️ 対処法:**\n{m.get('solution', '')}")
        else:
            sev = default_response.get("severity", "info")
            msg = default_response.get("message", "既知のパターンが見つかりませんでした。")
            if sev == "warning":
                st.warning(f"🟡 **{default_response.get('title', '')}**\n\n{msg}")
            else:
                st.info(f"🔵 **{default_response.get('title', '')}**\n\n{msg}")

    st.divider()
    with st.expander("📚 既知のエラーパターン一覧 (knowledge.json より)"):
        for p in patterns:
            st.markdown(f"**{p.get('title', '')}**")
            st.caption(f"検出キーワード: `{'`, `'.join(p.get('keywords', []))}`")
            st.markdown("---")


# ──────────────────────────────────────────────
# 7. 画面4: AI Code QA  [v1.0 追加]
# ──────────────────────────────────────────────

# チェックリスト定義（外部 JSON に切り出す場合は knowledge.json の "qa_checks" キーへ）
# 各エントリ: {"id": str, "category": str, "label": str, "detail": str}
_QA_CHECKS: list[dict] = [
    # ── カテゴリ1: 環境・システム安全性 ──
    {
        "id":       "wsl_path",
        "category": "🖥️ 環境・システム安全性の確認",
        "label":    "WSL パス変換ロジックが欠落していないか",
        "detail": (
            "Windows 側パス（例: `C:\\\\Users\\\\foo\\\\case`）を WSL マウントパス"
            "（`/mnt/c/Users/foo/case`）へ変換する処理が、コマンド実行前に必ず通過する"
            "ルート上に実装されているか確認してください。\n\n"
            "特に `subprocess` 呼び出し、`shutil.copytree` などのファイル操作、"
            "`pathlib.Path` の解決で Windows パスがそのまま WSL に渡っていないか"
            "を重点的にレビューしてください。"
        ),
    },
    {
        "id":       "heavy_loop",
        "category": "🖥️ 環境・システム安全性の確認",
        "label":    "毎ステップ全体の 3D メッシュを読み込む重いループ処理になっていないか",
        "detail": (
            "計算中の PC フリーズを防ぐため、Python 側でループを回しながら全体の"
            "3D メッシュデータ（`polyMesh/` や全タイムステップのボリュームデータ）を"
            "毎ステップ読み込む実装になっていないか確認してください。\n\n"
            "軽量化の原則: プレビューには `postProcessing/surfaces/` 下の"
            "2D 断面データのみ使用し、かつ `update_every` 間引き制御を通すこと。"
        ),
    },
    # ── カテゴリ2: 目的変数と評価指標 ──
    {
        "id":       "objective_ld",
        "category": "📐 目的変数と評価指標の確認",
        "label":    "最適化・評価のターゲットが「揚抗比 (L/D)」として正しく計算されているか",
        "detail": (
            "単なる揚力係数 `CL` や揚力 `Fl` を目的関数にすると、抗力が増大する方向に"
            "誤って最適化される危険があります。\n\n"
            "AI が生成したコードで `objective` や `fitness` 等の変数が定義されている場合、"
            "その計算式が `L/D = lift / drag`（または `CL / CD`）になっているかを"
            "必ず確認してください。`drag ≈ 0` の除算ゼロにも注意が必要です。"
        ),
    },
    # ── カテゴリ3: 物理現象と独自定義の整合性 ──
    {
        "id":       "jet_terminology",
        "category": "🌊 物理現象と独自定義の整合性",
        "label": (
            "壁面近傍の流れを判定する条件分岐・コメントで"
            "「付着ジェット」「剥離ジェット」という統一呼称が用いられているか"
        ),
        "detail": (
            "AI はしばしば `attached_flow` / `separated_flow` など英語ベースや"
            "独自の命名を使います。本研究では壁面付近の流体挙動の分類に"
            "**「付着ジェット」** および **「剥離ジェット」** という呼称を統一して"
            "用いています。\n\n"
            "コメント・変数名・ログ文字列でこの呼称からの乖離がないか確認し、"
            "必要であれば置換・コメント追記を行ってください。"
        ),
    },
    {
        "id":       "mode_definition",
        "category": "🌊 物理現象と独自定義の整合性",
        "label": (
            "円柱配列の干渉パターン分類が AI 独自のモード定義になっておらず、"
            "「擬似翼モード（Step 1 / Step 2）」という標準定義に沿っているか"
        ),
        "detail": (
            "AI は円柱配列の干渉パターンを独自にクラスタリング・命名することがあります"
            "（例: `Mode_A` / `Mode_B`、`cluster_0` / `cluster_1` など）。\n\n"
            "本研究で定義する **「擬似翼モード Step 1」**（前縁円柱が揚力を発生させる段階）"
            "および **「擬似翼モード Step 2」**（後縁円柱との協調揚力が最大化する段階）"
            "という区分に、AI のコードが沿っているか確認してください。\n\n"
            "モード判定のしきい値・ロジックも研究定義と一致しているか併せてレビューしてください。"
        ),
    },
]


def page_ai_code_qa() -> None:
    """
    AI 生成コード 検証チェックリスト画面。
    メンバーが AI に書かせたスクリプトや設定ファイルを実運用する前に
    人間が安全性を確認するための「関所」として機能する。
    """
    st.header("✅ AI生成コード 検証チェックリスト")
    st.caption(
        "AI が生成したスクリプト・設定ファイルを実行する前に、"
        "必ず全項目を人間がレビューしてチェックを入れてください。"
    )

    # ── 概要バナー ──
    with st.container(border=True):
        col_icon, col_text = st.columns([1, 8])
        with col_icon:
            st.markdown("## 🔒")
        with col_text:
            st.markdown(
                "**なぜこのチェックが必要か？**\n\n"
                "大規模言語モデル (LLM) は流暢なコードを生成しますが、"
                "「研究室固有の物理定義」「環境固有のパス規則」「メモリ安全性」"
                "については訓練データに含まれていないため、誤りが生じやすいです。"
                "このリストは、そのような **既知の落とし穴** を人間が系統的に確認する"
                "ためのガイドです。"
            )

    st.divider()

    # ── チェックリスト（カテゴリ別） ──
    # チェック状態は session_state で管理
    if "qa_states" not in st.session_state:
        st.session_state.qa_states = {item["id"]: False for item in _QA_CHECKS}

    current_category = None
    checked_count = 0

    for item in _QA_CHECKS:

        # カテゴリヘッダ（変わったときだけ表示）
        if item["category"] != current_category:
            current_category = item["category"]
            st.subheader(current_category)

        # チェックボックス本体
        state = st.checkbox(
            label    = item["label"],
            value    = st.session_state.qa_states[item["id"]],
            key      = f"qa_{item['id']}",
        )
        # session_state に書き戻す
        st.session_state.qa_states[item["id"]] = state

        if state:
            checked_count += 1

        # 詳細説明（折りたたみ）
        with st.expander("📋 確認観点・チェック方法", expanded=False):
            st.markdown(item["detail"])

    st.divider()

    # ── 合否サマリー ──
    total = len(_QA_CHECKS)
    progress = checked_count / total

    st.markdown(f"**確認進捗: {checked_count} / {total} 項目**")
    st.progress(progress)

    if checked_count == total:
        st.success(
            "✅ 検証完了：このAI生成コードは安全に実行できます。\n\n"
            "全ての確認項目をクリアしました。実行・マージを進めてください。"
        )
        st.balloons()
    elif checked_count == 0:
        st.info("👆 上のチェックボックスをレビューしながら順番に確認してください。")
    else:
        remaining = total - checked_count
        st.warning(
            f"⚠️ あと **{remaining} 項目** の確認が残っています。"
            "すべての項目にチェックが入るまで実行を保留してください。"
        )

    # ── リセットボタン ──
    st.divider()
    col_reset, col_spacer = st.columns([1, 3])
    with col_reset:
        if st.button("🔄 チェックをリセット", use_container_width=True):
            st.session_state.qa_states = {item["id"]: False for item in _QA_CHECKS}
            st.rerun()


# ──────────────────────────────────────────────
# 8. サイドバー & メインルーティング
# ──────────────────────────────────────────────

def setup_sidebar(config: dict) -> str:
    app_info = config.get("app", {})
    with st.sidebar:
        st.markdown("## 🌊 OpenFOAM")
        st.markdown("### Knowledge Platform")
        st.caption(f"v{app_info.get('version', '1.0.0')}")
        st.divider()

        page = st.radio(
            "ナビゲーション",
            [PAGE_SETUP, PAGE_RUN, PAGE_TROUBLE, PAGE_QA],
            label_visibility="collapsed",
        )

        st.divider()
        st.markdown("**設定ファイル**")
        st.markdown(f"{'✅' if CONFIG_PATH.exists() else '❌'} config.json")
        st.markdown(f"{'✅' if KNOWLEDGE_PATH.exists() else '❌'} knowledge.json")

        st.divider()
        # pyvista インストール状況をサイドバーにも表示
        st.markdown("**ライブラリ状況**")
        st.markdown(f"{'✅' if _PYVISTA_AVAILABLE else '⚠️'} pyvista")
        st.caption("pyvista 未インストールの場合\n`pip install pyvista` で導入可能")

        st.divider()
        st.caption(
            "📌 このプラットフォームは各ユーザーの\n"
            "ローカル PC 上で動作します。\n\n"
            "機密データは JSON ファイルで管理し、\n"
            "app.py にはハードコードしません。"
        )
    return page


def main() -> None:
    st.set_page_config(
        page_title="OpenFOAM Knowledge Platform",
        page_icon="🌊",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    config    = load_config()
    knowledge = load_knowledge()
    page      = setup_sidebar(config)

    if page == PAGE_SETUP:
        page_setup(config)
    elif page == PAGE_RUN:
        page_run_monitor(knowledge)
    elif page == PAGE_TROUBLE:
        page_troubleshoot(knowledge)
    elif page == PAGE_QA:
        page_ai_code_qa()


if __name__ == "__main__":
    main()
