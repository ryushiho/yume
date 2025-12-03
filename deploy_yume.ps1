param(
    [string]$Message
)

# ==== 기본 설정 ====
# 이 스크립트가 있는 폴더(= git repo 루트)로 이동
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

# yssh 사용 가정:
#  - PowerShell에서 yssh만 치면 비밀번호 없이 서버 접속됨
#  - 하지만 yssh "명령"은 인자로 안 먹고, 그냥 ssh만 여는 형태
# => 그래서 원격 명령은 파이프로 넘겨서 실행한다.
$remoteCommand = @"
cd /opt/yume
git pull
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

# ==== 5) yssh로 서버에서 git pull + 재시작 ====
Write-Host "`n--- 서버로 SSH 접속 (yssh + 파이프 사용) ---" -ForegroundColor Yellow
Write-Host "yssh를 열고, 표준입력으로 배포 명령을 흘려보낼게." -ForegroundColor DarkGray

# 여기서 $remoteCommand 문자열이 yssh를 통해 원격 셸에서 실행됨
$remoteCommand | yssh

if ($LASTEXITCODE -ne 0) {
    Write-Host "서버 쪽 배포 명령에서 에러가 난 것 같아." -ForegroundColor Red
    exit 1
}

Write-Host "`n=== 배포 완료! 유메 최신 버전이 서버까지 반영됐어. 으헤~ ===" -ForegroundColor Cyan
