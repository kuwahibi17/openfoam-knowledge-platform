# 🌊 OpenFOAM Knowledge Platform

**🌐 Language:** **English** | [日本語](README.md)

> An integrated platform that wraps the entire OpenFOAM workflow in a GUI and turns scattered analysis know-how into shared, reusable team knowledge.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.35%2B-red?logo=streamlit)](https://streamlit.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

### ⚙️ Setup — Case Parameter Configuration
Default values loaded from `config.json` populate each input field. The Reynolds number is computed automatically from the reference velocity and kinematic viscosity, and the turbulence model and number of parallel cores can be selected from the GUI.

![Setup screen 1](docs/screenshot_setup_1.png)
![Setup screen 2](docs/screenshot_setup_2.png)

---

### 🚀 Run & Monitor — Command Execution & Knowledge Panel
Clicking a button in the left column runs the corresponding OpenFOAM command through WSL. The right column displays, in real time, the command-specific tips and cautions loaded from `knowledge.json`.

![Run & Monitor screen](docs/screenshot_run.png)

---

### 🔍 Troubleshoot — Error Log Analysis
Paste a terminal error log and click the button: the text is matched against keyword patterns defined in `knowledge.json`, and the likely cause and remedy are presented. Logs saved on the Run screen can also be loaded with a single click.

![Troubleshoot screen](docs/screenshot_troubleshoot.png)

---

### ✅ AI Code QA — Safety Review Checklist for AI-Generated Code
A "gatekeeper" to pass before putting AI-generated scripts or configuration files into real use. Once all five items are checked, an execution-approved message (`st.success`) appears. Each item includes a collapsible panel with detailed review steps.

![AI Code QA screen 1](docs/screenshot_qa_1.png)
![AI Code QA screen 2](docs/screenshot_qa_2.png)

---

## 🎯 Background & Motivation

### The Problem: Knowledge Silos in Scrapbox

Our laboratory had been recording analysis know-how in Scrapbox. While it worked well as a personal memo tool, there was little incentive to actively read other members' pages. As a result, practical analysis techniques were rarely shared, and knowledge tended to stay locked inside individuals — a classic case of siloed, person-dependent expertise.

### The Solution: From "Sharing by Reading" to "Sharing Built Into the Workflow"

To solve this, I moved away from the passive approach of "sharing by having people read records" toward a new one: "embedding know-how into the very process of running an analysis." I built a platform for OpenFOAM users in the lab in which knowledge is shared naturally as part of the everyday analysis work.

### Key Design Focus: Defending Against AI Hallucination (Physical Errors)

This platform was **developed with the help of AI**. In recent years, using AI to assist development has rapidly become common in research and any work involving coding. Our lab is no exception, and the opportunities to write analysis code with AI assistance are only expected to grow.

In that context, I came to strongly feel the importance of guarding against AI's "plausible-sounding falsehoods."

**Recognizing the risk:** AI is strong at coding, but it does not know a lab's domain-specific physical definitions (e.g., lift-to-drag ratio, "pseudo-wing mode"). As a result, it can generate code that looks plausible but is actually based on incorrect premises — a hallucination.

**A systemic countermeasure (AI Code QA):** To address this, I implemented a **review checklist** that requires a human to verify AI-generated code before it runs.

- **Mandatory checks:** Items that encode the lab's own domain knowledge — for example, whether the lift-to-drag ratio is being prioritized, whether the "attached jet / separated jet" terminology is used correctly, and whether the classification follows the "pseudo-wing mode" convention.
- **Fail-safe:** Execution is not approved unless every item has been checked off.

> ⚠️ The physical definitions above (lift-to-drag ratio, attached/separated jet, pseudo-wing mode, etc.) are **sample items** included to illustrate the feature. In real use, each team is expected to register its own evaluation criteria and terminology in `_QA_CHECKS` inside `app.py` (and, in the future, in `knowledge.json`).

By **separating the checklist items (the definitions themselves) from the code so they can be swapped out later**, the "gatekeeper" that lets humans reliably catch AI errors becomes reusable across any research topic.

### Goals & Outlook

The intuitive front end is designed so that even members with little command-line or programming experience can get started with analysis easily. The aim is for know-how to be shared through the act of using the tool, ultimately improving the productivity of the whole lab.

That said, the project is currently at the stage where development and verification have been completed in my own environment; full deployment within the lab is still ahead. Through future operation and feature expansion, I aim to contribute to lowering the learning curve and standardizing analysis quality.

---

## 🛠 Tech Stack & Implementation Highlights

| Technology | In a nutshell | Role |
| :--- | :--- | :--- |
| **Streamlit** | Build a UI with Python alone | Web GUI framework (no server needed, runs locally) |
| **subprocess + WSL** | Drive Linux behind the scenes from Windows | Safe execution of OpenFOAM commands with error handling |
| **Regular expressions** | Pull just the needed numbers out of logs | Parse `pimpleFoam` output in real time to extract residual values |
| **matplotlib (Agg)** | Turn results into charts/images | Convert slice data into lightweight 2D images (with memory release) |
| **pandas** | Handle tabular data | Manage residual time-series data and visualize it on a log-scale chart |
| **JSON** | Externalize configuration | Manage parameters and know-how externally instead of hardcoding |

### Design Decisions

Beyond just making it work, I anticipated common pitfalls based on how the tool would actually be used in the lab.

- **Memory-safe design**
  Loading all of the heavy 3D data would freeze the PC, so only lightweight 2D cross-section images are displayed. Specifically, the full mesh is never loaded on the Python side; only the 2D slice data under `postProcessing/surfaces/` is used, and preview updates are throttled with a `step % N == 0` control to keep the load low.

- **WSL path conversion**
  Windows and Linux (WSL) write folder paths differently (`C:\foo` vs. `/mnt/c/foo`), so I added automatic conversion. Using `PureWindowsPath`, paths like `C:\foo\bar` are converted to `/mnt/c/foo/bar`, letting a Windows app drive OpenFOAM on WSL seamlessly.

- **Separation of confidential data**
  Lab-specific information is never written directly into the code; it is split into separate files that are kept out of GitHub. Domain-specific parameters and know-how are managed externally in `*.json` files and are never hardcoded into `app.py`. Files containing real data are excluded from Git tracking via `.gitignore`.

---

## 🗺 Development Roadmap

| Version | Content | Status |
| :--- | :--- | :--- |
| **v0.1** | UI mockup, JSON loading, layout | ✅ Done |
| **v0.2** | WSL-linked command execution via `subprocess`, error handling | ✅ Done |
| **v0.3** | Real-time residual monitoring, flow-field slice image preview | ✅ Done |
| **v1.0** | Safety review checklist for AI-generated code (first stable release) | ✅ Done |
| **v1.1** | Automatic case-template generation, real lift/drag graph loading | 🚧 In progress |

---

## 🚀 Setup & Launch

### Prerequisites
- Windows 11 + WSL2 (Ubuntu 22.04 recommended)
- OpenFOAM installed on the WSL side
- Python 3.10 or later

### Installation

```bash
git clone https://github.com/<your-username>/openfoam-knowledge-platform.git
cd openfoam-knowledge-platform
pip install -r requirements.txt
```

### Launch

```bash
streamlit run app.py
```

Then open `http://localhost:8501` in your browser.

---

## 📁 File Structure

```
.
├── app.py              # Main application (all features, ~1600 lines)
├── config.json         # Default parameters (dummy values, for public release)
├── knowledge.json      # Command know-how & trouble patterns (dummy values, for public release)
├── requirements.txt    # Dependencies
├── docs/               # Screenshots for the README
└── README.md
```

> **Security policy:**
> In real use, confidential parameters are stored in `config.local.json` / `knowledge.local.json`,
> which are excluded from Git tracking via `.gitignore`.

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.
