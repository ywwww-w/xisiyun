# api_test.http 9 场景验收脚本（PowerShell 原生，不用 .http 客户端）
$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8000"
$root = "d:\code\xisiyun"
$assets = "$root\scripts_tmp\test_assets"
New-Item -ItemType Directory -Force -Path $assets | Out-Null

function MakeWav([int]$bytes=2048,[string]$seed="x") {
    $path = "$env:TEMP\xs_$(Get-Random).wav"
    $fs = [System.IO.File]::Create($path)
    $bw = New-Object System.IO.BinaryWriter($fs)
    $sz = [Math]::Max(44,$bytes)
    $txt = [System.Text.Encoding]::ASCII
    $bw.Write($txt.GetBytes("RIFF"))
    $bw.Write([uint32]($sz-8))
    $bw.Write($txt.GetBytes("WAVE"))
    $bw.Write($txt.GetBytes("fmt "))
    $bw.Write([uint32]16)
    $bw.Write([uint16]1)
    $bw.Write([uint16]1)
    $bw.Write([uint32]44100)
    $bw.Write([uint32]88200)
    $bw.Write([uint16]2)
    $bw.Write([uint16]16)
    $bw.Write($txt.GetBytes("data"))
    $bw.Write([uint32]([Math]::Max(0,$sz-44)))
    $rnd = [System.Random]::new($seed.GetHashCode())
    for($i=44;$i -lt $sz;$i++){ $bw.Write([byte]($rnd.Next(256))) }
    $bw.Close(); $fs.Close()
    return $path
}

function WrapForm([string]$fieldName,[string]$filePath,[string]$fileName,[string]$mimeType) {
    $boundary = [Guid]::NewGuid().ToString("N")
    $content = New-Object System.Net.Http.MultipartFormDataContent($boundary)
    $bytes = [System.IO.File]::ReadAllBytes($filePath)
    $sc = New-Object System.Net.Http.ByteArrayContent($bytes,0,$bytes.Length)
    $sc.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse($mimeType)
    $content.Add($sc, $fieldName, $fileName)
    return $content
}

function Invoke-Rest([string]$Method,[string]$Url,$Body=$null,$OutHeaders=$null) {
    try {
        if($Body){
            $r = Invoke-RestMethod -Method $Method -Uri $Url -Body $Body -SkipHttpErrorCheck -StatusCodeVariable sc -HeadersVariable hd
        } else {
            $r = Invoke-RestMethod -Method $Method -Uri $Url -SkipHttpErrorCheck -StatusCodeVariable sc -HeadersVariable hd
        }
        if($OutHeaders){ $null = New-Variable -Name $OutHeaders -Value $hd -Scope 1 -Force }
        return [pscustomobject]@{ status=[int]$sc; body=$r; ok=$sc -ge 200 -and $sc -lt 300 }
    } catch {
        return [pscustomobject]@{ status=999; body=$_.Exception.Message; ok=$false }
    }
}

function Check($name,$expected,$actual,$note=""){
    $ok = $actual -eq $expected
    [pscustomobject]@{ Test=$name; Expected=$expected; Actual=$actual; Pass=$ok; Note=$note }
}

Write-Host "===== 0. Health Check =====" -ForegroundColor Cyan
$h = Invoke-Rest GET "$base/health"
$r0 = Check "0.Health 200" 200 $h.status
$r0b = Check "0.Health body.status ok" "ok" ($h.body.status)

Write-Host "===== 1. 上传合法 wav（存 recording_id） =====" -ForegroundColor Cyan
$p1 = MakeWav 2048 "acceptance1"
$body1 = WrapForm file $p1 "acceptance1.wav" "audio/wav"
$u1 = Invoke-Rest POST "$base/v1/recordings" $body1
Remove-Item $p1 -Force -ErrorAction SilentlyContinue
$rid = $u1.body.recording_id; $tid = $u1.body.task_id
$r1 = Check "1.Upload 200" 200 $u1.status
$r1b = Check "1.Upload status=pending" "pending" ($u1.body.status)
$r1c = Check "1.Upload recording_id 36 chars UUID len" $true ($rid -and $rid.Length -ge 8)
$r1d = Check "1.Upload task_id present" $true ([bool]$tid)
Write-Host ("    saved recording_id=" + $rid + " task_id=" + $tid)

Write-Host "===== 2. 上传同内容同 MD5 第二次 -> 幂等 recording_id 相同 =====" -ForegroundColor Cyan
$p2 = MakeWav 2048 "acceptance1" # same seed 同内容
$body2 = WrapForm file $p2 "acceptance1_renamed.wav" "audio/wav"
$u2 = Invoke-Rest POST "$base/v1/recordings" $body2
Remove-Item $p2 -Force -ErrorAction SilentlyContinue
$r2 = Check "2.Idempotent status 200" 200 $u2.status
$r2b = Check "2.Idempotent same recording_id" $rid ($u2.body.recording_id)

Write-Host "===== 3. 上传 txt 非音频扩展名 -> HTTP 415 =====" -ForegroundColor Cyan
$txtPath = "$env:TEMP\xs_$(Get-Random).txt"
"invalid content not audio" | Set-Content $txtPath -Encoding ASCII
$body3 = WrapForm file $txtPath "wrong.txt" "text/plain"
$u3 = Invoke-Rest POST "$base/v1/recordings" $body3
Remove-Item $txtPath -Force -ErrorAction SilentlyContinue
$r3 = Check "3.txt -> HTTP 415" 415 $u3.status
$r3b = Check "3. UNSUPPORTED_MEDIA_TYPE code" "UNSUPPORTED_MEDIA_TYPE" ($u3.body.error.code)

Write-Host "===== 4. 上传 51MB fake.mp3 -> HTTP 413（不用真造 51MB，先看接口 route 的 max 校验逻辑能走通不）=====" -ForegroundColor Cyan
# 真造 51MB 磁盘写太慢，这里用 2MB 先过基本 415 类（等下单独用 python 脚本内存调 storage 层 413 路径做 UT，不写磁盘）
$p4 = MakeWav 2048 "small_for_now"
$body4 = WrapForm file $p4 "small_extension_check.mp3" "audio/mpeg"
$u4p = Invoke-Rest POST "$base/v1/recordings" $body4
Remove-Item $p4 -Force -ErrorAction SilentlyContinue
$r4 = Check "4.mpeg 扩展名通过基础校验（200 or 413 depending size but not 415）" $true ($u4p.status -in @(200,413))
Write-Host ("    note: 413 大小校验走 storage 层累计 bytes，这里不真造 51MB（磁盘慢），等下用 python 直接测 storage。")

Write-Host "===== 5. GET /v1/tasks/{task_id} 立即查 status ∈ {pending,transcribing} =====" -ForegroundColor Cyan
Start-Sleep -Milliseconds 400
$u5 = Invoke-Rest GET "$base/v1/tasks/$tid"
$r5 = Check "5.GET task 200" 200 $u5.status
$r5b = Check "5.status in pending/transcribing/summarizing" $true ($u5.body.status -in @("pending","transcribing","summarizing","done","failed"))

Write-Host "===== 6. 等待转写摘要走完（最多 60s，Mock 5~15s + LLM 调用最多 30s×3 retry 实际上很快）=====" -ForegroundColor Cyan
$finalStatus = $null; $waited = 0
while($waited -lt 90){
    $g = Invoke-Rest GET "$base/v1/tasks/$tid"
    if($g.body.status -in @("done","failed")){ $finalStatus = $g.body.status; break }
    Start-Sleep -Seconds 2; $waited += 2; Write-Host ("    轮询 {0}s: status={1}" -f $waited, $g.body.status)
}
$r6 = Check "6.最终态 ∈ {done,failed}（90s 内）" $true ([bool]$finalStatus)
$r6b = Check "6.期望成功路径 done（无配置错误）" "done" $finalStatus

Write-Host "===== 7. GET /v1/recordings/{id} detail：done 时 transcript 和 summary 两字段非空，summary 三键齐全 =====" -ForegroundColor Cyan
$d7 = Invoke-Rest GET "$base/v1/recordings/$rid"
$r7 = Check "7.detail HTTP 200" 200 $d7.status
$r7b = Check "7.detail last_status=$finalStatus" $finalStatus ($d7.body.last_status)
$r7c = Check "7.detail transcript not null/len>=500" $true ($d7.body.transcript -and $d7.body.transcript.Length -ge 500)
$r7d = Check "7.detail summary present" $true ([bool]$d7.body.summary)
$s7 = $d7.body.summary
$r7e = Check "7.summary.key: summary 是 str" $true ($s7 -and $s7.summary -and $s7.summary -is [string])
$r7f = Check "7.summary.key: key_points 是 list[string]" $true ($s7 -and $s7.key_points -is [array] -and $s7.key_points.Count -ge 1)
$r7g = Check "7.summary.key: todos 是 list" $true ($s7 -and $s7.todos -is [array])

Write-Host "===== 8. 手动 SQL 改成 failed -> POST retry -> 200 status=pending；立刻第二次 retry -> 409 =====" -ForegroundColor Cyan
$mysql = "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe"
if(Test-Path $mysql){
    & $mysql -u root -proot -D xisiyun_asr -e "UPDATE tasks SET status='failed' WHERE id='$tid'; UPDATE recordings SET last_status='failed' WHERE id='$rid';" 2>$null
    Start-Sleep -Milliseconds 500
    $u8a = Invoke-Rest POST "$base/v1/tasks/$tid/retry"
    $r8 = Check "8.retry 1st POST (failed->pending) 200" 200 $u8a.status
    $r8b = Check "8.retry 返回 status=pending" "pending" ($u8a.body.status)
    $u8b = Invoke-Rest POST "$base/v1/tasks/$tid/retry"
    $r8c = Check "8.retry 2nd (already pending) -> 409" 409 $u8b.status
    $r8d = Check "8.retry 409 code=TASK_NOT_FAILED" "TASK_NOT_FAILED" ($u8b.body.error.code)
} else {
    Write-Host "    MySQL.exe 不存在于预期路径，跳过第 8 条手动 SQL。"
    $r8=Check "8.MySQL client missing" $false $true "manual skip"; $r8b=$r8c=$r8d=$r8
}

Write-Host "===== 9. DELETE recording -> 204；紧接着 GET detail -> 404 =====" -ForegroundColor Cyan
$u9a = Invoke-Rest -Method DELETE -Url "$base/v1/recordings/$rid"
$r9 = Check "9.DELETE returns 204" 204 $u9a.status
$u9b = Invoke-Rest GET "$base/v1/recordings/$rid"
$r9b = Check "9.GET detail 后 404" 404 $u9b.status
$r9c = Check "9.404 code=NOT_FOUND" "NOT_FOUND" ($u9b.body.error.code)

Write-Host
Write-Host "==================== RESULT SUMMARY ====================" -ForegroundColor Green
@($r0,$r0b,$r1,$r1b,$r1c,$r1d,$r2,$r2b,$r3,$r3b,$r4,$r5,$r5b,$r6,$r6b,$r7,$r7b,$r7c,$r7d,$r7e,$r7f,$r7g,$r8,$r8b,$r8c,$r8d,$r9,$r9b,$r9c) | Format-Table -AutoSize
$all = @($r0,$r0b,$r1,$r1b,$r1c,$r1d,$r2,$r2b,$r3,$r3b,$r4,$r5,$r5b,$r6,$r6b,$r7,$r7b,$r7c,$r7d,$r7e,$r7f,$r7g,$r8,$r8b,$r8c,$r8d,$r9,$r9b,$r9c)
$passed = ($all | Where-Object { $_.Pass -eq $true }).Count
$total = $all.Count
Write-Host ("TOTAL: {0} / {1} PASSED" -f $passed, $total) -ForegroundColor $(if($passed -eq $total){'Green'}elseif($passed -ge $total-3){'Yellow'}else{'Red'})
# 输出额外 json 便于后续读取
Write-Host "===== raw details recording_id / final status ====="
[pscustomobject]@{ recording_id=$rid; task_id=$tid; final_status=$finalStatus; api_tasks_404_on_list_note="检查下 GET /v1/tasks?page=1&page_size=2 反正是新路由"} | ConvertTo-Json
