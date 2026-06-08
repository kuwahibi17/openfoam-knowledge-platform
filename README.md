# 🌊 OpenFOAM Knowledge Platform

> 流体解析ソフト **OpenFOAM** の操作を GUI で完結させ、組織のノウハウを共有するための統合プラットフォーム

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.35%2B-red?logo=streamlit)](https://streamlit.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## 📸 スクリーンショット

### ⚙️ Setup — ケースパラメータ設定
`config.json` から読み込んだデフォルト値を初期値として各入力欄に表示。レイノルズ数・代表速度・動粘性係数から Re を自動計算し、乱流モデルや並列コア数を選択できる。

![Setup画面1](docs/screenshot_setup_1.png)
![Setup画面2](docs/screenshot_setup_2.png)

---

### 🚀 Run & Monitor — コマンド実行 & ナレッジパネル
左カラムのボタンを押すと WSL 経由で OpenFOAM コマンドを実行。右カラムには `knowledge.json` から読み込んだそのコマンド固有のノウハウ・注意事項をリアルタイムで表示する。

![Run & Monitor画面](docs/screenshot_run.png)

---

### 🔍 Troubleshoot — エラーログ解析
ターミナルのエラーログを貼り付けてボタンを押すと、`knowledge.json` に定義したキーワードパターンとマッチングし、原因と対処法を提示する。Run 画面で保存された実行ログをワンクリックで読み込むことも可能。

![Troubleshoot画面](docs/screenshot_troubleshoot.png)

---

### ✅ AI Code QA — AI生成コード 安全性検証チェックリスト
AI が生成したスクリプトや設定ファイルを実運用する前の「関所」。5項目すべてにチェックが入ると `st.success` で実行許可が表示される。各項目には折りたたみ式の詳細確認手順を配置。

![AI Code QA画面1](docs/screenshot_qa_1.png)
![AI Code QA画面2](docs/screenshot_qa_2.png)

---

## 🎯 開発背景と狙い

### 課題：Scrapbox による知識のサイロ化

研究室ではこれまで、解析ノウハウの記録に **Scrapbox** を活用していました。
個々の作業メモとしては機能する一方で、**「他のメンバーのページをわざわざ読みに行く機会が少ない」** という属人化の問題を抱えており、実践的な解析手法の共有が不十分でした。各々が自分のページに書き込むだけで、ノウハウが個人に閉じてしまっていたのです。

### 解決策：「読む」共有から「実行プロセスに溶け込む」共有へ

そこで、「記録を読む」という受動的な共有ではなく、**「解析を実行する過程で自然とノウハウが共有される新しい仕組み」** が必要だと考え、OpenFOAM を使うメンバー向けに本プラットフォームを開発しました。

### 最大の工夫：研究室独自の知見をシステムに組み込む

本ツールの核心は、単なる実行用 GUI を作ったことではなく、**研究室独自の知見やルールをシステムに組み込んだ点** にあります。

具体的には、研究室で標準とする以下の定義を、ツール内の **「検証チェックリスト（AI Code QA）」** として実装しました。

- 最適化の目的変数を **「揚抗比」** に統一する
- 壁面付近の流体挙動を **「付着ジェット」「剥離ジェット」** と正確に区別する
- 干渉パターンを **「擬似翼モード（Step 1 / Step 2）」** として分類する

これにより、メンバー間で評価軸や用語のズレがなくなり、**ツールを使うだけで全員が同じ基準で解析を行える環境** を実現しました。「自ら読みに行く」必要をなくし、標準化された解析手法が自然かつ確実に共有される仕組みです。

### 成果と今後の展望

直感的なフロントエンドを提供したことで、コマンドライン操作やプログラミング経験の浅いメンバーでも解析のハードルが大きく下がりました。結果として、システムを介して解析手法がスムーズに共有されるようになり、**チーム全体の研究効率の底上げ** に貢献できると考えています。

今後も機能を拡張し、研究室全体のデータの一貫性と解析スピードの向上を目指していきます。

---

## 🛠 技術スタック・実装のポイント

| 技術 | 用途 |
|------|------|
| **Streamlit** | Web GUI フレームワーク（サーバー不要・ローカル完結） |
| **subprocess + WSL 連携** | Windows から WSL 上の OpenFOAM コマンドを安全に実行。`CalledProcessError` を `check=True` で捕捉する安全装置を実装 |
| **正規表現ログパーサ** | `pimpleFoam` の stdout をリアルタイムパースし残差値（Ux, Uy, p, k, omega）を抽出・蓄積 |
| **matplotlib (Agg バックエンド)** | VTK 断面データを 2D カラーマップ画像として軽量レンダリング。`plt.close()` を徹底しメモリリークを防止 |
| **pandas** | 残差の時系列データを DataFrame で管理・対数軸グラフで可視化 |
| **JSON 外部設定** | パラメータ・ノウハウを `config.json` / `knowledge.json` で管理し、`app.py` にハードコードしない設計 |

### 設計上のこだわり

- **メモリ安全設計**：全体 3D メッシュは Python 側で読み込まず、`postProcessing/surfaces/` の 2D 断面データのみ使用。プレビュー更新は `step % N == 0` の間引き制御で負荷を抑制
- **WSL パス変換**：`PureWindowsPath` を用いて `C:\foo\bar` → `/mnt/c/foo/bar` を自動変換し、Windows/WSL の境界をシームレスに橋渡し
- **機密データ分離**：研究室固有のパラメータ・ノウハウは `*.json` で外部管理し、`app.py` 本体にはハードコードしない。`.gitignore` で実データを遮断

---

## 🗺 開発ロードマップ

| Phase | 内容 | 状態 |
|-------|------|------|
| **Phase 1** | UI モックアップ・JSON 読み込み・レイアウト | ✅ 完了 |
| **Phase 2** | `subprocess` による WSL 連携コマンド実行・エラーハンドリング | ✅ 完了 |
| **Phase 3** | 残差リアルタイム監視・流場スライス画像プレビュー | ✅ 完了 |
| **Phase 3+** | AI生成コード安全性検証チェックリスト画面 | ✅ 完了 |
| Phase 4 | ケーステンプレート自動生成・実揚抗力グラフ読み込み | 🔜 予定 |

---

## 🚀 セットアップ・起動方法

### 前提条件
- Windows 11 + WSL2（Ubuntu 22.04 推奨）
- WSL 側に OpenFOAM がインストール済み
- Python 3.10 以上

### インストール

```bash
git clone https://github.com/<your-username>/openfoam-knowledge-platform.git
cd openfoam-knowledge-platform
pip install -r requirements.txt
```

### 起動

```bash
streamlit run app.py
```

ブラウザで `http://localhost:8501` が開きます。

---

## 📁 ファイル構成

```
.
├── app.py              # メインアプリ（全機能・約1600行）
├── config.json         # デフォルトパラメータ（ダミー値・公開用）
├── knowledge.json      # コマンドノウハウ・トラブルパターン（ダミー値・公開用）
├── requirements.txt    # 依存ライブラリ
├── docs/               # README 用スクリーンショット
└── README.md
```

> **セキュリティ方針**：
> 実運用時の機密パラメータは `config.local.json` / `knowledge.local.json` に保存し、
> `.gitignore` によって Git 管理対象から除外しています。

---

## 📄 ライセンス

MIT License — 詳細は [LICENSE](LICENSE) を参照してください。
