param(
    [string]$Message
)

# ==== 기본 설정 ====
# 이 스크립트가 있는 폴더(= git repo 루트)로 이동
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

# 서버 접속 정보 (네 환경에 맞게 수정)
$serverUser = "root"
$serverHost = "vmi2949508"        # 또는 vmi2949508.yourhost.com / IP 등등
$server = "$serverUser@$serverHost"

# 서버에서 실행할 명령
# - /opt/yume 로 이동
# - git pull 로 최신 코드 받기
# - (필요하면 아래 systemctl 줄을 네가 쓰는 방식에 맞게 수정)
$remoteCommand = @"
cd /opt/yume && \
git pull && \
systemctl restart yume.service
"@

Write-Host "=== Yume 배포 스크립트 시작 ===" -ForegroundColor Cyan
Write-Host "작업 경로: $scriptDir" -ForegroundColor DarkGray

# ==== 1) 커밋 메시지 확보 ====
if (-not $Message) {
    $Message = Read-Host "커밋 메시지를 입력해줘 (엔터만 치면 자동 메시지 사용)"
    if (-not $Message) {
        $Message = "auto deploy $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    }
}

# ==== 2) git status 한번 보여주기 ====
Write-Host "`n--- git status ---" -ForegroundColor Yellow
git status

# ==== 3) 변경사항 커밋 ====
Write-Host "`n--- git add . ---" -ForegroundColor Yellow
git add .

Write-Host "`n--- git commit ---" -ForegroundColor Yellow
git commit -m "$Message"
if ($LASTEXITCODE -ne 0) {
    Write-Host "커밋이 안 됐어. (아마 변경 사항이 없을 수도 있어.)" -ForegroundColor DarkYellow
} else {
    Write-Host "커밋 완료: $Message" -ForegroundColor Green
}

# ==== 4) GitHub 로 push ====
Write-Host "`n--- git push origin main ---" -ForegroundColor Yellow
git push origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "git push 도중에 문제가 생겼어. 배포 중단." -ForegroundColor Red
    exit 1
}

Write-Host "`nGitHub push 완료!" -ForegroundColor Green

# ==== 5) 서버로 SSH 접속해서 git pull + 재시작 ====
Write-Host "`n--- 서버로 SSH 접속: $server ---" -ForegroundColor Yellow
Write-Host "서버 비밀번호 또는 SSH 키 패스프레이즈를 입력하라는 창이 뜰 수 있어." -ForegroundColor DarkGray

# Windows 10/11 기본 ssh 클라이언트를 사용
ssh $server "$remoteCommand"

if ($LASTEXITCODE -ne 0) {
    Write-Host "서버 쪽 배포 명령에서 에러가 난 것 같아." -ForegroundColor Red
    exit 1
}

Write-Host "`n=== 배포 완료! 유메 최신 버전이 서버까지 반영됐어. 으헤~ ===" -ForegroundColor Cyan
