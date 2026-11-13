# 小組協作程式使用手冊

七個指令可以在本機執行。你可以把這個資料夾交給 AI，並用下方的自然語言指令操作。程式提供 Git 和協作紀錄的機械操作，AI 負責判斷測試內容、審查結果和衝突解法。

## 先跑一次作業演練

在本機 Codex 的 PowerShell 貼上整段。測試會自己建立並清除測試用儲存庫，無需 GitHub 帳號。

```powershell
$runtime = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies"
$env:Path = "$runtime\native\git\cmd;$env:Path"
$env:PYTHONUTF8 = '1'
$pythonExe = "$runtime\python\python.exe"
$toolDir = "$env:USERPROFILE\Documents\Codex\2026-11-13\ai-1-2-sync-pub-int\outputs"
& $pythonExe --version
git --version
& $pythonExe -B -m unittest discover -s "$toolDir\tests" -v
```

若看到 `Ran 8 tests` 和 `OK`，就完成了本機驗證。測試使用三個模擬成員，涵蓋安全同步、發布可被發現、審查、正常合併、衝突保留與解決、通過和失敗的測試紀錄、新 clone 交接、候選報告及確認後封存。組員若已安裝 Git 與 Python 3.10 以上，可以在此資料夾直接用 `python -B -m unittest discover -s tests -v`。

## 開始使用小組的 GitHub 儲存庫

1. 在 GitHub 建立或取得小組共用儲存庫並 clone 到電腦。每位組員先確認有自己的 GitHub 權限。將此資料夾的兩支 Python 程式及代理指引放到 clone 的根目錄，並把它們納入小組的 Git 版本。
2. 請 AI 依老師的 `02_repository_contract.md` 初始化 `.group/`，或從儲存庫根目錄執行下方的設定指令。把共享設定提交到標準分支。
3. 每位同學建立自己的 `people/<person_id>/<topic>` 分支。保留 `people/<person_id>/events` 給工具存放協作紀錄。設定 Git 的 `user.name`、`user.email`，並在 clone 內確認 `git remote -v` 的 `origin` 指向小組儲存庫。
4. 在 clone 根目錄執行 `python groupctl.py STATUS`。若沿用上面的 Codex PowerShell 設定，可執行 `& $pythonExe "$toolDir\groupctl.py" STATUS`。看到 `remote_fresh: true`、正確的 `group_id` 和 `canonical_commit`，表示可讀取遠端小組狀態。在 `main` 上可加 `--person P17`，將 P17 改成自己的 ID。

首次設定可用這行。將作業檔路徑、群組 ID、成員 ID 與姓名替換成實際值，按組員數重複 `--member`。若組內已有 `.group/`，直接用現有設定。

```powershell
& $pythonExe "$toolDir\setup_group.py" --group G-12 --branch main --assignment 'C:\你的資料夾\assignment.md' --member P17 '小明' --member P22 '小華'
```

可以把這句話貼給新 AI 對話。

> 請在我的小組 GitHub clone 內讀取 AGENT_COLLABORATION.md 和老師的規格，執行 STATUS，並用中文告訴我目前版本、未提交的工作、相關事件和建議的下一步。

## 七個指令怎麼操作

下列指令開頭的 `python groupctl.py` 可換成前述 PowerShell 寫法。請 AI 把提交、變更 ID 和測試命令替換成實際值。每個子指令都能用 `--help` 看參數。

| 對 AI 說 | 工具操作 | 怎麼驗證 |
| --- | --- | --- |
| **STATUS** | `STATUS` | 比對 `git status --short`、遠端標準提交、ahead/behind 及事件列表。 |
| **SYNC** | `SYNC` 或加快速檢查 `--check '["{python}","-m","unittest","discover","-s","tests"]'` | 看 `action` 和 `new_events`。可快轉且檢查通過才會更新；未提交工作或分岔時只更新資訊。 |
| **PUB** | 先 `PUB --inspect`。確認適當變更已提交後，用 `PUB --confirm-commit <提交> --summary <摘要> --check <檢查>` | 取得 `change_id`。請另一位組員 SYNC 或 STATUS，看是否能找到發布事件。 |
| **REV C-…** | 先 `REV <變更ID>`。審閱後再加 `--confirm-commit <來源提交> --verdict <結論> --summary <摘要>` | STATUS 可看到 review；審查者目前分支的提交維持原樣。 |
| **INT C-…** | 先 `INT <變更ID>`。確認後加 `--apply --confirm-commit <來源提交>`。驗證成功後另跑 `INT <變更ID> --record success --confirm-commit <來源提交> --semantic-note <審查摘要> --check <檢查>` | 看 Git 歷史與 integration_result。遇到衝突請讓 AI 先解讀兩邊需求，必要時用 `--record needs_human` 回報。 |
| **TEST**、**TEST main** 或 **TEST C-…** | `TEST <範圍> --check <檢查>`。可重複提供 `--check`。 | 看 `test_result` 的提交、命令、結果及人工檢查。未指定檢查會發布 `not_available`。 |
| **FINAL** | 先 `FINAL` 看待處理事項。AI 準備報告後用 `FINAL --report <報告檔> --confirm-commit <標準提交> --check <檢查>`。看過候選報告並同意後才用 `FINAL --confirm-candidate <事件ID>`。 | 先確認候選階段未更新標準分支。確認後檢查標準分支下的 `.group/final/final.json` 與 `.group/final/final_report.md`。 |

`--check` 是一組 JSON 參數陣列，`{python}` 會使用目前執行工具的 Python。只執行你信任且適用於本專案的檢查。工具在暫存 Git 副本執行測試，測試次數欄位以命令數計。測試案例數請由測試框架的輸出判讀。

## 小組合併與最後提交

- 把驗證成功的個人分支送成 GitHub pull request，由組員確認後併入標準分支。選擇保留原提交的合併方式，例如 GitHub 的 **Create a merge commit**。工具用提交的祖先關係確認貢獻是否已被納入。
- FINAL 以標準分支為準。若有待整合變更或 blocker，AI 需逐項評估。能結案的項目用 `--disposition-id` 與 `--disposition` 說明，重要問題仍開放時先處理問題。已知限制用 `--issue` 記錄。
- 若 GitHub 的標準分支禁止直接推送，確認候選時加 `--via-pr`。依輸出的 `branch` 和 `base` 到 GitHub 建立 PR。合併前再次核對候選提交仍是最新標準狀態。只有標準分支上的 `final_result` 會被 STATUS 視為已生效。
- 協作事件存成老師規定的 `.group/events/<event_id>.json`，通常集中在成員的事件分支。請保留這些分支與已發布的工作分支，讓新 clone 能復原紀錄與引用的提交。已發布事件如需修正，應新增事件並指向原紀錄。
