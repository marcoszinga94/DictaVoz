# Confía en la firma de DictaVoz (certificado de Marcos Zingaretti) para tu
# usuario de Windows y desbloquea los .exe descargados (Mark-of-the-Web).
# No requiere administrador.
#
# Uso (en la carpeta con DictaVoz.cer y la de descargas):
#   powershell -ExecutionPolicy Bypass -File confiar_dictavoz.ps1
#   powershell -ExecutionPolicy Bypass -File confiar_dictavoz.ps1 -ExePath ".\DictaVoz-Setup.exe"
#
param(
  [string]$CertPath = (Join-Path $PSScriptRoot 'DictaVoz.cer'),
  [string]$ExePath   = (Join-Path $PSScriptRoot 'DictaVoz-Setup.exe')
)

if (-not (Test-Path $CertPath)) {
  Write-Host "No se encuentra el certificado: $CertPath (descárgalo del release)."
  exit 1
}

# 1) Instala el certificado como raíz y editor de confianza (solo tu usuario).
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($CertPath)
Write-Host "Certificado: $($cert.Subject)"
foreach ($storeName in @('Root', 'TrustedPublisher')) {
  $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($storeName, 'CurrentUser')
  $store.Open('ReadWrite')
  try { $store.Add($cert) } catch { Write-Host "  ($storeName ya tenía el certificado)" }
  $store.Close()
  Write-Host "Añadido a $storeName (CurrentUser)"
}

# 2) Quita el "bloqueo de descarga" (Mark-of-the-Web) a los .exe.
foreach ($target in @($ExePath, (Join-Path $PSScriptRoot 'DictaVoz.exe'))) {
  if ($target -and (Test-Path $target)) {
    try { Unblock-File -Path $target -ErrorAction Stop; Write-Host "Desbloqueado: $target" }
    catch { Write-Host "  (no se pudo desbloquear: $($_.Exception.Message))" }
  }
}

# 3) Verifica la firma.
if (Test-Path $ExePath) {
  $sig = Get-AuthenticodeSignature -FilePath $ExePath
  Write-Host ''
  Write-Host "Firma de $ExePath -> $($sig.Status)  Editor: $($sig.SignerCertificate.Subject)"
  if ($sig.Status -eq 'Valid') { Write-Host '✅ La firma ahora es válida en tu equipo.' }
  else { Write-Host 'Si la firma aún no es válida, cierra y abre de nuevo el archivo (o revisa la pestaña Firma digital).' }
}

Write-Host ''
Write-Host 'Listo. Si el navegador/Windows seguían avisando, abre ahora el .exe desde el Explorador.'