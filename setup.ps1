# DAG Flow 一键安装脚本（下载所有依赖：前端 npm 包、后端 Python 包、可选 RAG 模型）
# 双击 setup.cmd，或运行：
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
#
# 用法：
#   setup.ps1              # 安装基础依赖（npm + pip）
#   setup.ps1 -WithRag     # 额外安装 sentence-transformers + bge 语义模型（体积大，需要网络）
#
param(
    [switch]$WithRag
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Join-Path $Root 'backend'
$PyMirror = 'https://pypi.tuna.tsinghua.edu.cn/simple'
$NpmMirror = 'https://registry.npmmirror.com/'

function Step([string]$text) {
    Write-Host ''
    Write-Host $text -ForegroundColor Cyan
}

# ---------- 1. 前端依赖 ----------
Step '[1/4] 安装前端依赖（npm）...'
Push-Location $Root
try {
    npm config set registry $NpmMirror
    npm install
    if ($LASTEXITCODE -ne 0) { throw "npm install 失败" }
} finally {
    Pop-Location
}
Write-Host '  前端依赖安装完成' -ForegroundColor Green

# ---------- 2. 后端依赖 ----------
Step '[2/4] 安装后端依赖（pip）...'
Push-Location $BackendDir
try {
    python -m pip install --upgrade pip -i $PyMirror
    python -m pip install -r requirements.txt -i $PyMirror
    if ($LASTEXITCODE -ne 0) { throw "pip install 失败" }
} finally {
    Pop-Location
}
Write-Host '  后端依赖安装完成' -ForegroundColor Green

# ---------- 3/4. 可选 RAG 依赖 ----------
if ($WithRag) {
    Step '[3/4] 安装 RAG 依赖（sentence-transformers + tokenizers）...'
    python -m pip install "sentence-transformers==5.4.1" "tokenizers==0.22.2" -i $PyMirror
    Write-Host '  RAG 依赖安装完成' -ForegroundColor Green

    Step '[4/4] 下载本地语义模型 bge-small-zh-v1.5（约 96MB）...'
    $ModelFile = Join-Path $Root 'models\bge-small-zh-v1.5\model.safetensors'
    if (Test-Path $ModelFile) {
        Write-Host '  模型已存在，跳过下载' -ForegroundColor Green
    } else {
        Write-Host '  通过 hf-mirror.com 分段下载模型（可能需要几分钟）...' -ForegroundColor Yellow
        Push-Location $BackendDir
        try {
            python download_model.py
            if ($LASTEXITCODE -ne 0) { throw "模型下载脚本失败" }
        } finally {
            Pop-Location
        }
        Write-Host '  模型下载完成' -ForegroundColor Green
    }
} else {
    Step '[3/4] 跳过 RAG 依赖（如需语义检索，运行：setup.ps1 -WithRag）'
}

Step '全部依赖安装完成！'
Write-Host '  - 启动：双击 start-dev.cmd'
Write-Host '  - 前端：http://localhost:5173'
Write-Host '  - 后端：http://localhost:8000/docs'
