# 🎮 AI 派蒙 — 详细部署指南

> 本文档将指导你从零开始部署 AI 派蒙语音助手。

---

## 📋 目录

- [系统要求](#系统要求)
- [第一步：安装基础环境](#第一步安装基础环境)
- [第二步：部署 Open-LLM-VTuber](#第二步部署-open-llm-vtuber)
- [第三步：配置 OpenClaw (ClawBot)](#第三步配置-openclaw-clawbot)
- [第四步：部署 Paimon VITS 语音](#第四步部署-paimon-vits-语音)
- [第五步：整合配置](#第五步整合配置)
- [第六步：启动服务](#第六步启动服务)
- [常见问题排查](#常见问题排查)

---

## 系统要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10/11 (也支持 Linux/macOS) |
| Python | 3.10 或更高版本 |
| 内存 | ≥ 8 GB RAM |
| 磁盘 | ≥ 2 GB 可用空间 |
| 网络 | 需要互联网（首次下载 ASR 模型，以及 LLM API 调用） |

> 💡 GPU 非必需。VITS 和 ASR 均支持 CPU 推理。如有 NVIDIA GPU 可自动加速。

---

## 第一步：安装基础环境

### 1.1 安装 Python

从 [python.org](https://www.python.org/downloads/) 下载并安装 Python 3.10+。
安装时勾选 **"Add Python to PATH"**。

### 1.2 安装 uv（推荐）

```powershell
pip install uv
```

### 1.3 安装 OpenClaw CLI

按照 [OpenClaw 官方文档](https://openclaw.com/) 安装。确保 `openclaw` 命令可用：

```powershell
openclaw --version
```

---

## 第二步：部署 Open-LLM-VTuber

```powershell
git clone https://github.com/Open-LLM-VTuber/Open-LLM-VTuber.git
cd Open-LLM-VTuber
uv sync
```

---

## 第三步：配置 OpenClaw (ClawBot)

### 3.1 启动 Gateway

```powershell
openclaw gateway
```

### 3.2 获取凭据

启动后，你的配置文件位于 `~/.openclaw/openclaw.json`。
从中获取以下信息并填入 `.env` 文件：

- `token` → `OPENCLAW_TOKEN`
- `deviceId` → `OPENCLAW_DEVICE_ID`
- 私钥 → `OPENCLAW_PRIVATE_KEY`

### 3.3 验证 Gateway

浏览器打开 `http://127.0.0.1:18789/`，应能看到 Gateway 信息页面。

---

## 第四步：部署 Paimon VITS 语音

### 4.1 克隆本仓库

```powershell
git clone https://github.com/gaaiyun/ai-paimon.git
cd ai-paimon
```

### 4.2 安装依赖

```powershell
pip install -r requirements.txt
```

### 4.3 放置模型文件

将 `paimon.pth` 复制到仓库根目录：

```
ai-paimon/
├── paimon.pth    ← 放在这里
└── ...
```

### 4.4 配置环境变量

```powershell
copy .env.example .env
# 用文本编辑器打开 .env，填入你的 OpenClaw 凭据
```

### 4.5 测试 VITS 服务

```powershell
python src/vits_server/server.py
```

看到 `✅ VITS model loaded successfully!` 表示成功。

---

## 第五步：整合配置

### 5.1 复制配置文件

```powershell
copy config\conf.yaml.example <Open-LLM-VTuber路径>\conf.yaml
copy config\model_dict.json <Open-LLM-VTuber路径>\model_dict.json
```

### 5.2 编辑 conf.yaml

打开 `conf.yaml`，将 `YOUR_OPENCLAW_GATEWAY_TOKEN` 替换为你的实际 token。

---

## 第六步：启动服务

需要按顺序启动 3 个服务：

```mermaid
graph LR
    A["1. openclaw gateway"] --> B["2. VITS TTS Server"]
    B --> C["3. Open-LLM-VTuber"]
    C --> D["🌐 浏览器访问<br/>localhost:12393"]
```

### 手动启动

```powershell
# 终端 1
openclaw gateway

# 终端 2
cd ai-paimon
python src/vits_server/server.py

# 终端 3
cd Open-LLM-VTuber
uv run run_server.py
```

### 一键启动 (Windows)

```powershell
cd ai-paimon
scripts\start_all.bat
```

---

## 常见问题排查

### Q: VITS 启动报 `ModuleNotFoundError`

```powershell
pip install -r requirements.txt
```

### Q: Gateway 连接失败

确认 `openclaw gateway` 已启动，且 `.env` 中的 token 正确。

### Q: 语音合成没声音

检查 `conf.yaml` 中 `tts_model` 是否设为 `x_tts`，且 `api_url` 指向 `http://127.0.0.1:8020/tts_to_audio`。

### Q: 想切回 Edge TTS

编辑 `conf.yaml`：

```yaml
tts_config:
  tts_model: 'edge_tts'
  edge_tts:
    voice: zh-CN-XiaoxiaoNeural
```

### Q: VITS 推理很慢

在 CPU 上推理一句话大约需要 2-5 秒。如有 NVIDIA GPU，安装 CUDA 版 PyTorch 即可自动加速。

---

*最后更新: 2026-05-02*
