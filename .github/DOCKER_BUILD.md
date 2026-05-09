# GitHub Actions – Docker Build

Aquesta acció compila automàticament les imatges Docker per a **múltiples arquitectures**:
- ✅ `linux/amd64` (Intel/AMD x64)
- ✅ `linux/arm64/v8` (Raspberry Pi 4B, 5)
- ✅ `linux/arm/v7` (Raspberry Pi Zero, 3)

## Configuració

### 1. Crear un **Personal Access Token** a Docker Hub

1. Ve a https://hub.docker.com/settings/security
2. Clica **New Access Token**
3. Nom: `camsec-github-actions` (o el que vulguis)
4. **Read & Write** (o menys si vols)
5. Copia el token

### 2. Afegir secrets a GitHub

1. Ve al repositori → **Settings** → **Secrets and variables** → **Actions**
2. Clica **New repository secret**
3. Crea dos secrets:

| Nom | Valor |
|---|---|
| `DOCKER_USERNAME` | El teu usuari de Docker Hub |
| `DOCKER_PASSWORD` | El token que vas copiar |

### 3. (Opcional) Habilitar GHCR

Si vols usar **GitHub Container Registry** (gratis, privat):
- No calen configuracions extra (usa `GITHUB_TOKEN`)
- Les imatges es pugen a `ghcr.io/<usuari>/camsec-<servei>`

## Com funciona

L'acció es dispara automàticament quan:

1. **Fas push a `main`**
   ```bash
   git push origin main
   ```
   → Compila i publica com `latest`

2. **Crees un tag de versió**
   ```bash
   git tag v1.0.0
   git push origin v1.0.0
   ```
   → Compila i publica com `v1.0.0`, `v1.0`, `v1`, `latest`

3. **Obris un Pull Request**
   → Compila sense pujar (test solament)

## Imatges generades

Després de cada push a `main`, tindràs disponibles:

### Docker Hub (si configures secrets)
```
docker pull aurelinoura/camsec-detector:latest
docker pull aurelinoura/camsec-viewer:latest
```

### GitHub Container Registry (automàtic)
```
docker pull ghcr.io/aurelinoura/camsec-detector:latest
docker pull ghcr.io/aurelinoura/camsec-viewer:latest
```

## Usar les imatges en Raspberry Pi

```bash
# Detecta automàticament l'arquitectura ARM
docker pull ghcr.io/aurelinoura/camsec-detector:latest

# Descàrrega i corre directament
docker run -d \
  --name camsec-detector \
  -e MQTT_BROKER=192.168.1.10 \
  ghcr.io/aurelinoura/camsec-detector:latest
```

## Veure el status

1. Ve al repositori → **Actions**
2. Veuràs els workflows en execució/completats
3. Clica en qualsevol per veure els logs detallats

## Solució de problemes

| Error | Causa | Solució |
|---|---|---|
| `invalid reference format` | Secrets no configurats | Afegir `DOCKER_USERNAME` i `DOCKER_PASSWORD` |
| `permission denied` | Token sense permisos | Regenerar token amb `Read & Write` |
| `platform not found` | Dockerfile incompatible | Verificar que Dockerfile suporta `FROM --platform` |

---

**Nota:** La primera build trigarà 15-20 min per compilar 3 arquitectures. Les properes usaran caché i seran més ràpides (3-5 min).
