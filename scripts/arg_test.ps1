param(
  [Parameter(ValueFromRemainingArguments=$true)]
  [string[]]$Arguments
)
Write-Output ('TYPE=' + $Arguments.GetType().FullName)
Write-Output ('COUNT=' + $Arguments.Count)
foreach ($a in $Arguments) { Write-Output ('ARG=' + $a) }
