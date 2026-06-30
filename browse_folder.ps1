Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = "Chon thu muc luu video"
$d.RootFolder = "MyComputer"
$d.ShowNewFolderButton = $true
if ($d.ShowDialog() -eq "OK") {
    $d.SelectedPath
} else {
    "CANCELLED"
}
