# 用 Windows 自带 OCR（WinRT，无需装任何东西）识别图片文字，输出「文字 + 像素包围盒」。
# 用途：把试卷/拓扑截图里的设备名、IP、VLAN、接口号连位置一起量出来，
#       再交给 figure_probe.py / make_topo.py --calibrate 复现拓扑。
#
#   powershell -NoProfile -File ocr_win.ps1 -Path 图.png
#   powershell -NoProfile -File ocr_win.ps1 -Path 图.png -Scale 2     # 小字先放大
#
# 输出格式（每行）:  x1,y1-x2,y2  WxH  <文字>
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [int]$Scale = 1
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($op, [Type]$resultType) {
    $task = $asTaskGeneric.MakeGenericMethod($resultType).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime]

if ($Scale -gt 1) {
    Add-Type -AssemblyName System.Drawing
    $src = [System.Drawing.Image]::FromFile((Resolve-Path $Path))
    $bmp = New-Object System.Drawing.Bitmap ($src.Width * $Scale), ($src.Height * $Scale)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.DrawImage($src, 0, 0, $bmp.Width, $bmp.Height)
    $tmp = [IO.Path]::Combine($env:TEMP, "ocr_" + [IO.Path]::GetFileNameWithoutExtension($Path) + "_x$Scale.png")
    $bmp.Save($tmp, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose(); $src.Dispose()
    $Path = $tmp
}

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Resolve-Path $Path).Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { Write-Error "系统没有可用的 OCR 语言包"; exit 1 }

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $result.Lines) {
    $x1 = 1e9; $y1 = 1e9; $x2 = -1; $y2 = -1
    foreach ($w in $line.Words) {
        $r = $w.BoundingRect
        $wx1 = [double]$r.X; $wy1 = [double]$r.Y
        $wx2 = $wx1 + [double]$r.Width; $wy2 = $wy1 + [double]$r.Height
        if ($wx1 -lt $x1) { $x1 = $wx1 }
        if ($wy1 -lt $y1) { $y1 = $wy1 }
        if ($wx2 -gt $x2) { $x2 = $wx2 }
        if ($wy2 -gt $y2) { $y2 = $wy2 }
    }
    $x1 = [int]($x1 / $Scale); $y1 = [int]($y1 / $Scale)
    $x2 = [int]($x2 / $Scale); $y2 = [int]($y2 / $Scale)
    "{0},{1}-{2},{3}  {4}x{5}  {6}" -f $x1, $y1, $x2, $y2, ($x2 - $x1), ($y2 - $y1), $line.Text
}