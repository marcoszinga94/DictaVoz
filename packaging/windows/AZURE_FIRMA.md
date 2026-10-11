# Firma oficial con Azure Trusted Signing (gratis)

Esto hace que Windows reconozca DictaVoz como "editor conocido" en **cualquier**
equipo, sin SmartScreen ni pasos de confianza. Es la única vía gratuita y oficial.

## Pasos (una vez, ~10 min)

1. Crea una cuenta de Azure (no paga nada por esto): https://azure.microsoft.com/free
2. Instala la CLI y entra:
   ```
   az login
   az account set --subscription "<tu-suscripcion>"
   ```
3. Crea el grupo de recursos y el **Trusted Signing Account**:
   ```
   az group create -n dictavoz-rg -l eastus
   az trustedsigning create -g dictavoz-rg -n DictaVozSigning \
       -l eastus --sku Basic
   ```
4. Crea el **perfil de certificado** (Identity Validation puede tardar unos días
   la primera vez):
   ```
   az trustedsigning certificate-profile create -g dictavoz-rg \
       -a DictaVozSigning -n DictaVozProfile \
       --profile-type PublicTrust --identity-validation-id <ID>
   ```
5. Crea una aplicación de servicio (Service Principal) para el CI:
   ```
   az ad sp create-for-rbac -n "dictavoz-ci" \
       --scopes /subscriptions/<sub>/resourceGroups/dictavoz-rg \
       --role "Trusted Signing Certificate Profile Signer" \
       --sdk-auth
   ```
   Guarda `clientId`, `tenantId` y `clientSecret` de la salida.

6. Añade estos secrets al repo (Settings → Secrets and variables → Actions:
   - `AZURE_TENANT_ID` = tenantId
   - `AZURE_CLIENT_ID` = clientId
   - `AZURE_CLIENT_SECRET` = clientSecret
   - `AZURE_ENDPOINT` = `https://eus.codesigning.azure.net` (región us-east)
   - `AZURE_ACCOUNT` = `DictaVozSigning`
   - `AZURE_PROFILE` = `DictaVozProfile`

7. Haz un push (o la acción manual "Windows installer"). El workflow detecta los
   secrets y firma con **Azure Trusted Signing** en vez de autofirmada.

El binario saldrá firmado por la CA de Microsoft → "editor conocido" en Windows
sin ningún aviso.