# Confía en la firma de DictaVoz (self-signed) para tu usuario de Windows.
# Elimina el aviso de "editor desconocido"/SmartScreen para el .exe firmado.
# No requiere administrador: se instala en los almacenes del usuario.
#
# Uso:
#   powershell -ExecutionPolicy Bypass -File confiar_dictavoz.ps1
#   (debe estar DictaVoz.cer en la misma carpeta, o pásale la ruta)
#
param(
  [string]$CertPath = (Join-Path $PSScriptRoot 'DictaVoz.cer')
)

if (-not (Test-Path $CertPath)) {
  Write-Host "No se encuentra el certificado: $CertPath"
  exit 1
}

$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($CertPath)
Write-Host "Certificado: $($cert.Subject)"

foreach ($storeName in @('Root', 'TrustedPublisher')) {
  $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($storeName, 'CurrentUser')
  $store.Open('ReadWrite')
  $store.Add($cert)
  $store.Close()
  Write-Host "Añadido a $storeName (CurrentUser)"
}

Write-Host ''
Write-Host 'Listo: DictaVoz queda marcada como aplicación CONFIABLE para tu usuario.'
Write-Host 'Los .exe firmados con este certificado abrirán sin avisos.'