# GitHub Push 가이드

이 zip은 git 저장소가 이미 초기화되어 있고 첫 커밋과 remote까지 설정된 상태입니다.
**사용자님 PC에서 push 한 번만** 실행하면 GitHub에 올라가요.

## 0. 압축 해제

```bash
unzip jarvis.zip
cd jarvis
git status   # 깨끗한 작업 트리, main 브랜치 확인
git log      # "feat: S.A.R.V.I.S personal AI assistant..." 커밋 확인
git remote -v  # origin → https://github.com/hobbong21/jarvis.git
```

## 1. GitHub 인증 설정 (둘 중 하나 선택)

### 옵션 A — Personal Access Token (HTTPS, 추천)

1. https://github.com/settings/tokens 이동
2. "Generate new token (classic)" 클릭
3. Note: `sarvis-push`, Expiration: 30일 정도, **scopes: `repo` 체크**
4. Generate token → 토큰 복사 (한 번만 보임)
5. push 시 username은 GitHub 사용자명 (`hobbong21`), password는 토큰

캐시해두려면:
```bash
git config --global credential.helper store   # 평문 저장 (개인 PC만)
# 또는 macOS:
git config --global credential.helper osxkeychain
# 또는 Windows:
git config --global credential.helper manager
```

### 옵션 B — SSH Key

1. SSH 키 생성 (없다면):
   ```bash
   ssh-keygen -t ed25519 -C "hobbong21@github"
   cat ~/.ssh/id_ed25519.pub
   ```
2. 출력된 공개키를 https://github.com/settings/keys 에 등록
3. remote URL을 SSH로 변경:
   ```bash
   git remote set-url origin git@github.com:hobbong21/jarvis.git
   ```

## 2. 본인 정보로 커밋 작성자 변경 (선택)

zip 안의 커밋은 "Sarvis Setup <sarvis@local>" 명의로 되어 있어요. 본인 명의로 바꾸려면:

```bash
git config user.email "본인이메일@example.com"
git config user.name "본인이름"
git commit --amend --reset-author --no-edit
```

## 3. Push

```bash
git push -u origin main
```

토큰 인증이라면 username/password 입력 프롬프트가 뜹니다:
- Username: `hobbong21`
- Password: 위에서 만든 토큰 붙여넣기

## 만약 GitHub 저장소에 이미 파일이 있다면

(예: 저장소 생성 시 README나 LICENSE를 자동 생성한 경우)

```bash
# 옵션 1: 강제 푸시 (GitHub 쪽 내용 덮어씀, 주의!)
git push -u origin main --force

# 옵션 2: GitHub 쪽 내용 가져와서 합치기
git pull origin main --rebase --allow-unrelated-histories
# 충돌 해결 후
git push -u origin main
```

## 검증

푸시 성공하면:
- https://github.com/hobbong21/jarvis 접속해서 README가 보이는지 확인
- 14개 파일 (LICENSE, README.md, *.py, requirements.txt, .gitignore) 확인

## 트러블슈팅

**"remote: Repository not found"** — 저장소가 비공개거나 이름 오타. GitHub에서 저장소가 존재하고 사용자명이 `hobbong21`인지 확인.

**"Permission denied (publickey)"** — SSH 키가 등록 안 됐거나 잘못됨. `ssh -T git@github.com`으로 테스트.

**"failed to push some refs"** — GitHub 쪽에 이미 커밋이 있음. 위 "이미 파일이 있다면" 섹션 참고.

**"401 Unauthorized"** — 토큰 만료 또는 권한 부족. 토큰 재발급 또는 scope 확인.
