param([switch]$Apply)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$expectedRoot = 'D:\cypher\GTPR\TransitMind'
if ($projectRoot -ne $expectedRoot) {
    throw 'This migration is scoped to D:\cypher\GTPR\TransitMind.'
}
$evalRoot = (Resolve-Path -LiteralPath (Join-Path $projectRoot 'artifacts\eval')).Path
$sourcePrefix = $evalRoot.TrimEnd('\') + '\'
$legacyRoot = [IO.Path]::GetFullPath((Join-Path $evalRoot 'legacy'))
$destinationPrefix = $legacyRoot.TrimEnd('\') + '\'
$baselineRoot = Join-Path $legacyRoot 'baseline-20261028'
$followupRoot = Join-Path $legacyRoot 'followup-20261029'
$supplements = @(Get-ChildItem -LiteralPath (Join-Path $followupRoot 'grading') -Directory -Filter 'supplement-d10-*')
if ($supplements.Count -ne 1) {
    throw 'Expected exactly one explicitly requested D10 scoring supplement.'
}
$baselineReportName = 'report-scorer-v4-20261029T055636188550Z.json'
$baseline = Get-Content -LiteralPath (Join-Path $evalRoot $baselineReportName) -Encoding UTF8 -Raw | ConvertFrom-Json
$followup = Get-Content -LiteralPath (Join-Path $evalRoot 'latest-report.json') -Encoding UTF8 -Raw | ConvertFrom-Json
$d10RunId = 'D10-20261029T075713'
$baselineIds = @($baseline.records | ForEach-Object run_id)
$followupIds = @($followup.records | ForEach-Object run_id) + $d10RunId
if ($baselineIds.Count -ne 30 -or $followupIds.Count -ne 30 -or $d10RunId -in @($followup.records | ForEach-Object run_id)) {
    throw 'The two source reports no longer match the reviewed 30-case rounds.'
}

$plan = [Collections.Generic.List[object]]::new()
function Get-Sha256 {
    param([string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '')
    } finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Add-PlannedMove {
    param([string]$Source, [string]$Destination, [string]$Round, [string]$Kind)
    $resolvedSource = (Resolve-Path -LiteralPath $Source).Path
    $resolvedDestination = [IO.Path]::GetFullPath($Destination)
    # 每一个源路径和目标路径先核对绝对范围；不跨 shell，不覆盖同名目标。
    if (-not $resolvedSource.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not $resolvedDestination.StartsWith($destinationPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'A move would leave the approved evaluation directory.'
    }
    if (Test-Path -LiteralPath $resolvedDestination) {
        throw "Destination already exists: $resolvedDestination"
    }
    $plan.Add([pscustomobject]@{
        source = $resolvedSource
        destination = $resolvedDestination
        round = $Round
        kind = $Kind
        sha256 = Get-Sha256 -Path $resolvedSource
    })
}

foreach ($round in @(
    @{ name = 'baseline'; root = $baselineRoot; ids = $baselineIds },
    @{ name = 'followup'; root = $followupRoot; ids = $followupIds }
)) {
    foreach ($runId in $round.ids) {
        $source = Join-Path $evalRoot ($runId + '.json')
        $trace = Get-Content -LiteralPath $source -Encoding UTF8 -Raw | ConvertFrom-Json
        if ($trace.run_id -ne $runId) {
            throw "Trace run ID does not match its source report: $runId"
        }
        Add-PlannedMove -Source $source -Destination (Join-Path $round.root ('traces\' + $runId + '.json')) -Round $round.name -Kind 'trace'
    }
}

foreach ($file in Get-ChildItem -LiteralPath (Join-Path $evalRoot 'records') -File -Filter '*.json') {
    $record = Get-Content -LiteralPath $file.FullName -Encoding UTF8 -Raw | ConvertFrom-Json
    if ($record.run_id -in $baselineIds) {
        Add-PlannedMove -Source $file.FullName -Destination (Join-Path $baselineRoot ('grading\saved-records\' + $file.Name)) -Round 'baseline' -Kind 'score_record'
    } elseif ($record.run_id -in $followupIds) {
        Add-PlannedMove -Source $file.FullName -Destination (Join-Path $followupRoot ('grading\original\records\' + $file.Name)) -Round 'followup' -Kind 'score_record'
    }
}

$olderReport = Get-Content -LiteralPath (Join-Path $evalRoot 'report-scorer-v2.json') -Encoding UTF8 -Raw | ConvertFrom-Json
if (@($olderReport.records | Where-Object { $_.run_id -notin $baselineIds }).Count -ne 0) {
    throw 'The older scoring report contains answers outside the baseline.'
}
Add-PlannedMove -Source (Join-Path $evalRoot $baselineReportName) -Destination (Join-Path $baselineRoot ('grading\' + $baselineReportName)) -Round 'baseline' -Kind 'report'
Add-PlannedMove -Source (Join-Path $evalRoot 'report-scorer-v2.json') -Destination (Join-Path $baselineRoot 'grading\report-scorer-v2.json') -Round 'baseline' -Kind 'historical_report'
Add-PlannedMove -Source (Join-Path $evalRoot 'manual-review-v1.json') -Destination (Join-Path $baselineRoot 'reviews\manual-review-v1.json') -Round 'baseline' -Kind 'review'
Add-PlannedMove -Source (Join-Path $evalRoot 'latest-report.json') -Destination (Join-Path $followupRoot 'grading\original\latest-report.json') -Round 'followup' -Kind 'report'
if (@($plan | Group-Object source | Where-Object Count -gt 1).Count -ne 0) {
    throw 'The migration plan contains duplicate source files.'
}
foreach ($roundRoot in @($baselineRoot, $followupRoot)) {
    if (Test-Path -LiteralPath (Join-Path $roundRoot 'manifest.json')) {
        throw "Archive manifest already exists: $roundRoot"
    }
}

[pscustomobject]@{
    source_root = $evalRoot
    destination_root = $legacyRoot
    file_count = $plan.Count
    groups = @($plan | Group-Object round,kind | Select-Object Name,Count)
    apply = [bool]$Apply
} | ConvertTo-Json -Depth 4
if (-not $Apply) {
    return
}

$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssffffffZ')
$utf8 = [Text.UTF8Encoding]::new($false)
[IO.Directory]::CreateDirectory($legacyRoot) | Out-Null
$migrationPath = Join-Path $legacyRoot ('migration-' + $stamp + '.json')
$migration = [ordered]@{
    format_version = 'legacy-migration-v1'
    created_at = [DateTime]::UtcNow.ToString('o')
    status = 'planned'
    operation = 'move'
    files = @($plan.ToArray())
}
[IO.File]::WriteAllText($migrationPath, ($migration | ConvertTo-Json -Depth 8), $utf8)

foreach ($entry in $plan) {
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($entry.destination)) | Out-Null
    Move-Item -LiteralPath $entry.source -Destination $entry.destination
    $actualHash = Get-Sha256 -Path $entry.destination
    if ($actualHash -ne $entry.sha256 -or (Test-Path -LiteralPath $entry.source)) {
        throw "Move verification failed; consult $migrationPath"
    }
}

# 历史答卷保持字节不变，不补写新的 batch_id，也不冒充新格式的原生批次。
foreach ($round in @(
    @{ name = 'baseline'; root = $baselineRoot; report = ('grading/' + $baselineReportName); ids = $baselineIds },
    @{ name = 'followup'; root = $followupRoot; report = 'grading/original/latest-report.json'; ids = $followupIds }
)) {
    $manifest = [ordered]@{
        format_version = 'legacy-archive-v1'
        round_id = [IO.Path]::GetFileName($round.root)
        archived_at = [DateTime]::UtcNow.ToString('o')
        operation = 'move'
        original_report = $round.report
        run_ids = @($round.ids)
        trace_count = $round.ids.Count
        moved_file_count = @($plan | Where-Object round -eq $round.name).Count
        migration_manifest = ('../' + [IO.Path]::GetFileName($migrationPath))
        native_batch = $false
        note = 'Original JSON and run IDs are unchanged. Use archived reports; native --batch rescore does not import legacy archives.'
    }
    if ($round.name -eq 'baseline') {
        $manifest['reference_report'] = $round.report
        $manifest['original_case_record_files_available'] = @($plan | Where-Object { $_.round -eq 'baseline' -and $_.kind -eq 'score_record' }).Count
        $manifest['record_note'] = 'The complete 30-case scoring data is embedded in the reference report; 29 original per-case files had already been overwritten before archiving.'
    } else {
        $manifest['reference_report'] = 'grading/' + $supplements[0].Name + '/report.json'
        $manifest['original_scoring_error'] = @{ case_id = 'D10'; stage = 'judge'; type = 'ModelCallError'; message = 'DeepSeek request timed out. Check the network before retrying.' }
        $manifest['pending_value_reviews'] = @('B4', 'B5', 'B6', 'B8', 'C1')
    }
    [IO.File]::WriteAllText((Join-Path $round.root 'manifest.json'), ($manifest | ConvertTo-Json -Depth 6), $utf8)
}
$migration['status'] = 'completed'
$migration['verified_files'] = $plan.Count
$migration['finished_at'] = [DateTime]::UtcNow.ToString('o')
[IO.File]::WriteAllText($migrationPath, ($migration | ConvertTo-Json -Depth 8), $utf8)
Write-Output "Moved and SHA256-verified $($plan.Count) files. Migration manifest: $migrationPath"
