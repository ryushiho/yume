# yumepush.ps1
# YumeBot 배포 스크립트 2.0
# 로컬 프로젝트 전체를 tar.gz로 묶어서 서버 /opt/yume 에 배포하고
# systemd 서비스(yume.service)를 재시작한다.

param(
    [string]$Server = "46.250.252.119",     # ← 서버 IP/도메인으로 수정
    [string]$User = "root",                 # ← SSH 유저 (보통 root)
    [string]$RemotePath = "/opt/yume",      # ← 서버에서 Yume가 설치된 경로
    [string]$ServiceName = "yume.service"   # ← systemd 서비스 이름
)

# --- 설정 시작 ---

# 로컬 Yume 프로젝트 루트 경로
$ProjectRoot = "C:\Users\henna\yumebot"

# 배포용 tar.gz 파일 이름 (임시 파일)
$ArchiveName = "yume_deploy.tar.gz"
$ArchivePath = Join-Path $ProjectRoot $ArchiveName

# tar로 묶을 때 제외할 항목들
$TarExclude = @(
    ".git"
    "venv"
    ".vscode"
    ".idea"
    "__pycache__"
    "node_modules"
)

# --- 설정 끝 ---

Write-Host "==== [YumePush 2.0] YumeBot 배포 시작 ====" -ForegroundColor Cyan

# 1. 로컬 프로젝트 경로 확인
if (-not (Test-Path $ProjectRoot)) {
    Write-Host "[ERROR] ProjectRoot 경로가 존재하지 않습니다: $ProjectRoot" -ForegroundColor Red
    exit 1
}

Set-Location $ProjectRoot
Write-Host "ProjectRoot: $ProjectRoot"

# 2. 이전 배포 파일 삭제
if (Test-Path $ArchivePath) {
    Write-Host "기존 배포 파일 삭제: $ArchivePath"
    Remove-Item $ArchivePath -Force
}

# 3. tar.gz 생성
Write-Host "프로젝트를 tar.gz로 압축 중..."

# tar exclude 옵션 조합
$excludeArgs = @()
foreach ($ex in $TarExclude) {
    $excludeArgs += "--exclude=$ex"
}

# tar 실행 (Windows에는 기본적으로 tar 있음 - bsdtar)
# 현재 폴더(.) 기준으로 압축
$tarArgs = @(
    "-czf", $ArchivePath
) + $excludeArgs + @(".")

Write-Host "tar " ($tarArgs -join " ")
$tarResult = & tar @tarArgs 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] tar 압축 중 오류 발생:" -ForegroundColor Red
    Write-Host $tarResult
    exit 1
}

Write-Host "압축 완료: $ArchivePath" -ForegroundColor Green

# 4. 서버로 tar.gz 전송 (scp)
Write-Host "서버로 파일 전송 중 (scp)..."
$remoteArchive = "/tmp/$ArchiveName"
$scpArgs = @($ArchivePath, "$User@$Server:$remoteArchive")

Write-Host "scp $ArchivePath $User@$Server:$remoteArchive"
$scpResult = & scp @scpArgs 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] scp 전송 중 오류 발생:" -ForegroundColor Red
    Write-Host $scpResult
    exit 1
}

Write-Host "파일 전송 완료: $remoteArchive" -ForegroundColor Green

# 5. 서버에서 배포 & 서비스 재시작 (ssh)
Write-Host "서버에서 배포 절차 실행 (ssh)..."

$remoteCommands = @(
    "set -e",
    "echo '1) $ServiceName 중지 중...'",
    "sudo systemctl stop $ServiceName || true",
    "",
    "echo '2) 배포 디렉토리 생성: $RemotePath'",
    "sudo mkdir -p $RemotePath",
    "",
    "echo '3) tar 풀기...'",
    "sudo tar -xzf $remoteArchive -C $RemotePath",
    "",
    "echo '4) 임시 파일 삭제...'",
    "sudo rm -f $remoteArchive",
    "",
    "echo '5) $ServiceName 시작...'",
    "sudo systemctl start $ServiceName",
    "",
    "echo '6) 서비스 상태 확인:'",
    "sudo systemctl status $ServiceName --no-pager -n 5 || true",
    "",
    "echo '배포 완료.'"
) -join " && "

$sshArgs = @("$User@$Server", $remoteCommands)

Write-Host "ssh $User@$Server '<commands>'"
$sshResult = & ssh @sshArgs 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] ssh 과정에서 경고/오류 발생(로그 확인 필요):" -ForegroundColor Yellow
    Write-Host $sshResult
} else
{
    Write-Host "서버 배포 및 서비스 재시작 완료." -ForegroundColor Green
}

# 6. 로컬 임시 tar.gz 삭제 (선택)
if (Test-Path $ArchivePath) {
    Write-Host "로컬 임시 파일 삭제: $ArchivePath"
    Remove-Item $ArchivePath -Force
}

Write-Host "==== [YumePush 2.0] 완료 ====" -ForegroundColor Cyan
