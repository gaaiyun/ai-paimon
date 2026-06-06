# 🎮 AI 派蒙 — 完整部署指南

> 本文档将手把手指导你从零开始，在本地完整运行 AI 派蒙语音助手。

---

## 📋 目录

- [系统要求](#系统要求)
- [整体架构说明](#整体架构说明)
- [第一步：安装基础环境](#第一步安装基础环境)
- [第二步：部署 Open-LLM-VTuber 引擎](#第二步部署-open-llm-vtuber-引擎)
- [第三步：配置 Paimon Live2D 模型](#第三步配置-paimon-live2d-模型)
- [第四步：部署 Paimon VITS 语音服务](#第四步部署-paimon-vits-语音服务)
- [第五步：配置 OpenClaw 并获取 Token](#第五步配置-openclaw-并获取-token)
- [第六步：整合 conf.yaml 配置](#第六步整合-confyaml-配置)
- [第七步：启动全部服务](#第七步启动全部服务)
- [常见问题排查](#常见问题排查)

---

## 系统要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10/11（推荐）；Linux/macOS 亦可 |
| Python | 3.10 或更高版本 |
| 内存 | ≥ 8 GB RAM |
| 磁盘 | ≥ 3 GB 可用空间（含 VITS 模型权重 ~417MB） |
| 网络 | 需要互联网（首次下载 ASR 模型，以及 LLM API 调用） |

> 💡 **不需要 GPU**。VITS 和 ASR 均支持纯 CPU 推理，但如有 NVIDIA GPU 可自动加速。

---

## 整体架构说明

```
用户麦克风/文字输入
        ↓
   浏览器 :12393  ←── Open-LLM-VTuber 主服务（调度器）
        ↓ WebSocket
   SenseVoice ASR（本地 CPU，语音识别）
        ↓
   ClawBot Bridge :5001（本仓库，OpenAI 兼容 → OpenClaw WS v3）
        ↓
   OpenClaw Gateway :18789（本地 → 调用你配置好的大模型）
        ↓
   Paimon VITS TTS :8020（本地 CPU/GPU，合成派蒙音色）
        ↓
   Live2D 派蒙模型（浏览器渲染，含表情/动作同步）
```

**所有 AI 推理走 OpenClaw**，因此你在 OpenClaw 里配置了哪个大模型（MiniMax、Qwen、Claude 等），派蒙就用那个脑子说话，**并且共享同一份记忆上下文**。

---

## 第一步：安装基础环境

### 1.1 安装 Python

从 [python.org](https://www.python.org/downloads/) 下载 Python 3.10+，安装时勾选 **"Add Python to PATH"**。

### 1.2 安装 uv（推荐用于 Open-LLM-VTuber）

```powershell
pip install uv
```

### 1.3 安装 Git

从 [git-scm.com](https://git-scm.com/) 下载安装。

### 1.4 安装 OpenClaw CLI

按照 [OpenClaw 官方文档](https://openclaw.com/) 安装 ClawBot。安装完成后确认命令可用：

```powershell
openclaw --version
```

---

## 第二步：部署 Open-LLM-VTuber 引擎

AI 派蒙使用 **Open-LLM-VTuber** 作为核心调度引擎，需要单独克隆并安装：

```powershell
git clone https://github.com/Open-LLM-VTuber/Open-LLM-VTuber.git
cd Open-LLM-VTuber
uv sync
```

> ⚠️ 安装过程会自动下载 ASR 模型（约 400MB），请确保网络畅通。

记住这个目录的路径（以下用 `<OLV_DIR>` 代替），例如：
```
C:\Projects\Open-LLM-VTuber
```

---

## 第三步：配置 Paimon Live2D 模型

本仓库已包含完整的派蒙 Live2D 模型文件（位于 `live2d-models/paimon/`）。

### 3.1 克隆本仓库

```powershell
git clone https://github.com/gaaiyun/ai-paimon.git
cd ai-paimon
```

### 3.2 复制 Live2D 文件到引擎目录

```powershell
# 将派蒙 Live2D 模型复制到 Open-LLM-VTuber 的模型目录
Copy-Item -Recurse "live2d-models\paimon" "<OLV_DIR>\live2d-models\paimon"
```

### 3.3 复制 model_dict.json

```powershell
# 覆盖引擎的模型列表配置，注册派蒙模型
Copy-Item "config\model_dict.json" "<OLV_DIR>\model_dict.json"
```

> `model_dict.json` 中已预设了派蒙模型的所有参数（缩放比例、情绪映射、触碰动作等），复制后引擎会自动识别 `paimon` 为可用模型。

---

## 第四步：部署 Paimon VITS 语音服务

VITS 服务负责将派蒙的文字回复合成为派蒙音色的语音。

### 4.1 安装依赖

在 `ai-paimon` 目录下：

```powershell
pip install -r requirements.txt
```

### 4.2 获取 paimon.pth 模型权重

> ⚠️ `paimon.pth`（约 417MB）因文件过大，不包含在本仓库中。

获取方式：
- 从 [DigitalLife 项目](https://github.com/AnyaCoder/DigitalLife) 或原神 VITS 相关社区下载派蒙模型权重。
- 将下载到的 `paimon.pth` 放置于仓库根目录：

```
ai-paimon/
├── paimon.pth   ← 放在这里
├── config/
├── src/
└── ...
```

### 4.3 测试 VITS 服务是否正常

```powershell
python src/vits_server/server.py
```

看到以下输出表示成功：
```
✅ VITS 模型加载完成!
使用设备: cpu
🚀 启动 VITS API 服务，端口 8020
```

> 💡 首次启动大约需要 20-60 秒加载模型（CPU 模式），请耐心等待。

---

## 第五步：配置 OpenClaw 并获取 Token

### 5.1 启动 OpenClaw Gateway

在一个新终端中运行：

```powershell
openclaw gateway
```

成功启动后，Gateway 会监听在 `http://127.0.0.1:18789`。

### 5.2 找到你的 Gateway Token

OpenClaw 会在以下路径自动生成配置文件：

```
C:\Users\<你的用户名>\.openclaw\openclaw.json
```

用文本编辑器打开它，找到 `token` 字段：

```json
{
  "token": "eeecbae913a3c58291c42e61e21d9a8e041568f5e67f45b6",
  ...
}
```

复制这个 token 值，后面要用。

> ⚠️ **每次重新安装或初始化 OpenClaw 都可能生成新 token**，如果遇到 `401 Unauthorized` 错误，请重新从此文件获取最新 token。

### 5.3 验证 Gateway 正常运行

浏览器访问 `http://127.0.0.1:18789/`，能看到页面即表示 Gateway 正在运行。

---

## 第六步：整合 conf.yaml 配置

### 6.1 复制配置文件

```powershell
Copy-Item "config\conf.yaml.example" "<OLV_DIR>\conf.yaml"
```

### 6.2 编辑 conf.yaml — 填入 OpenClaw Token

打开 `<OLV_DIR>\conf.yaml`，找到如下部分并填入你的实际 token：

```yaml
      openai_compatible_llm:
        base_url: 'http://127.0.0.1:5001/v1'        # ClawBot Bridge（本仓库），不需要修改
        llm_api_key: 'not-needed'                    # 桥接器不校验此值，保持非空即可
        organization_id: null
        project_id: null
        model: 'clawbot'                              # 固定值，不需要修改
        temperature: 0.7
```

> Open-LLM-VTuber 通过本仓库的 **ClawBot Bridge**（`:5001`，OpenAI 兼容）接入 OpenClaw，
> 而不是直连 Gateway 的 `:18789`。真正的 OpenClaw 凭据（token / 设备密钥）配置在 `.env` 里，
> 由桥接器读取，因此这里的 `llm_api_key` 只需保持非空。启动桥接：`python src/clawbot_bridge.py`
> 或运行 `scripts\start_all.bat` 一键拉起。

### 6.3 确认 VITS TTS 配置（通常不需要修改）

在同一个 `conf.yaml` 文件中确认以下配置正确：

```yaml
  tts_config:
    tts_model: 'x_tts'
    x_tts:
      api_url: 'http://127.0.0.1:8020/tts_to_audio'  # Paimon VITS 服务
      speaker_wav: 'female'
      language: 'zh'
```

### 6.4 确认 Live2D 角色配置

```yaml
character_config:
  live2d_model_name: 'paimon'   # 必须与 model_dict.json 中的 name 一致
  character_name: '派蒙'
```

---

## 第七步：启动全部服务

必须按照以下顺序依次启动四个服务：

```
1. openclaw gateway   →   2. ClawBot Bridge (:5001)   →   3. Paimon VITS TTS (:8020)   →   4. Open-LLM-VTuber
```

### 方式一：一键启动（Windows 推荐）

编辑 `scripts\start_all.bat`，将其中的路径替换为你实际的路径，然后双击运行即可。该脚本会先健康检查 Gateway，再依次拉起 ClawBot Bridge、VITS 与 Open-LLM-VTuber。

### 方式二：手动分终端启动

**终端 1** — 启动 OpenClaw Gateway：
```powershell
openclaw gateway
```

**终端 2** — 启动 ClawBot Bridge（OpenClaw WS → OpenAI REST）：
```powershell
cd ai-paimon
python src/clawbot_bridge.py
```

**终端 3** — 启动 Paimon VITS 语音服务：
```powershell
cd ai-paimon
python src/vits_server/server.py
```

**终端 4** — 启动 Open-LLM-VTuber 主服务：
```powershell
cd <OLV_DIR>
uv run run_server.py
```

### 访问界面

四个服务全部启动后，打开浏览器访问：

**http://localhost:12393**

你应该能看到派蒙的 Live2D 模型出现在屏幕右侧，点击麦克风按钮或在输入框打字即可开始对话！🎉

---

## 可选：使用 PaimonPet 桌面宠物

除了浏览器界面，你还可以使用 [PaimonPet 桌面版](https://github.com/gaaiyun/paimon-pet)，获得更好的桌面体验：

- 透明置顶窗口，派蒙常驻桌面
- 精灵图动画（闲置/倾听/思考/说话/拖拽）
- 点击派蒙打开聊天面板，支持语音和文字
- 系统托盘菜单
- 一键启动后端服务

### 安装 PaimonPet

```powershell
# 在 ai-paimon 同级目录下克隆
cd ..  # 回到 paimon pet/ 目录
git clone https://github.com/gaaiyun/paimon-pet.git
cd paimon-pet
npm install
npx tauri dev
```

> PaimonPet 会自动检测同级目录下的 `ai-paimon` 和 `Open-LLM-VTuber` 路径。

---

## 常见问题排查

### ❓ 出现 `401 Unauthorized` 错误

**原因**：`conf.yaml` 中的 `llm_api_key` 与 OpenClaw Gateway 当前使用的 token 不匹配。

**解决**：
1. 打开 `~/.openclaw/openclaw.json`
2. 复制 `token` 字段的值
3. 粘贴到 `<OLV_DIR>/conf.yaml` 的 `llm_api_key` 字段
4. 重启 Open-LLM-VTuber 服务

---

### ❓ 派蒙 Live2D 没有显示（界面黑屏或空白）

**检查**：
- 确认已将 `live2d-models/paimon/` 目录完整复制到 `<OLV_DIR>/live2d-models/paimon/`
- 确认已将 `config/model_dict.json` 复制到 `<OLV_DIR>/model_dict.json`
- 确认 `conf.yaml` 中 `live2d_model_name: 'paimon'` 拼写正确

---

### ❓ 派蒙没有声音 / 说话声音不像派蒙

**检查**：
- 确认 VITS 服务正在运行（终端中看到 "VITS API 服务"）
- 确认 `paimon.pth` 文件已放置在 `ai-paimon/` 根目录
- 确认 `conf.yaml` 中 `tts_model` 设置为 `x_tts`，`api_url` 为 `http://127.0.0.1:8020/tts_to_audio`

如果临时没有 VITS 服务，可以改用 Edge TTS（无派蒙音色但可测试对话功能）：
```yaml
tts_config:
  tts_model: 'edge_tts'
  edge_tts:
    voice: zh-CN-XiaoxiaoNeural
```

---

### ❓ VITS 语音合成很慢

CPU 推理每句话约需 2-10 秒，这是正常现象。如有 NVIDIA GPU，安装 CUDA 版 PyTorch 后会大幅加速：

```powershell
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

---

### ❓ OpenClaw Gateway 无法启动

确认 OpenClaw 已正确安装，且你的 ClawBot 账号已登录。详见 [OpenClaw 官方文档](https://openclaw.com/)。

---

*最后更新: 2026-05-07*
