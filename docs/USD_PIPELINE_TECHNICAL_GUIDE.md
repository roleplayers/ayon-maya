# AYON Maya USD Pipeline - Guia Tecnico

> Documento tecnico detalhado dos plugins USD desenvolvidos para o pipeline
> AYON + Maya 2025.3 + MayaUSD. Cobre o fluxo completo: importacao de
> referencias USD em shots, criacao de rigs USD e exportacao de point cache
> (LayCache) para composicao de sublayers.

---

## Indice

1. [Visao Geral da Arquitetura](#1-visao-geral-da-arquitetura)
2. [Plugin 1: USD Add Reference (Loader)](#2-plugin-1-usd-add-reference-loader)
3. [Plugin 2: Maya USD Rig (Creator + Extractor)](#3-plugin-2-maya-usd-rig-creator--extractor)
4. [Plugin 3: Animation Cache USD / LayCache (Creator + Extractor)](#4-plugin-3-animation-cache-usd--laycache-creator--extractor)
5. [Composicao USD: Como as Layers se Encaixam](#5-composicao-usd-como-as-layers-se-encaixam)
6. [Fluxo Completo: Do Asset ao Shot Animado](#6-fluxo-completo-do-asset-ao-shot-animado)
7. [API e Utilitarios](#7-api-e-utilitarios)
8. [Troubleshooting e Edge Cases](#8-troubleshooting-e-edge-cases)

---

## 1. Visao Geral da Arquitetura

### Stack Tecnologico

| Componente         | Versao / Detalhes                          |
|--------------------|--------------------------------------------|
| Maya               | 2025.3                                     |
| MayaUSD Plugin     | >= 0.21.0 (para worldspace export)         |
| AYON               | Pipeline manager (containers, versioning)  |
| pxr (OpenUSD)      | Sdf, Usd, UsdGeom via Maya USD             |
| Pyblish            | Framework de publish (collect/validate/extract) |

### Arquivos do Pipeline

```
client/ayon_maya/
  plugins/
    load/
      load_maya_usd_add_reference.py    # Loader: adiciona refs USD ao shot
      load_maya_usd.py                  # Loader: proxy shape basico
      load_maya_usd_add_maya_reference.py  # Loader: MayaReference prims
    create/
      create_maya_usd_rig.py            # Creator: instancia de rig USD
      create_animation_cache_usd.py     # Creator: instancia de cache USD
    publish/
      collect_maya_usd_rig.py           # Collector: dados do rig
      collect_animation_cache_usd.py    # Collector: dados do cache
      validate_animation_cache_usd.py   # Validators: cache
      extract_maya_usd_rig.py           # Extractor: rig -> .mb + MayaReference
      extract_animation_cache_usd.py    # Extractor: cache -> point cache USD
      extract_maya_usd.py               # Extractor: USD generico
  api/
    usdlib.py                           # Utilitarios USD (containers, UFE)
    chasers/
      export_filter_properties.py       # Chaser para filtrar properties
```

### Diagrama de Composicao USD do Shot

```
shot.usda (Root Layer)
  |
  +-- [sublayer] rigging.usda
  |     \-- MayaReference prim -> rig.mb
  |
  +-- [sublayer] usdLayCache.usd (Point Cache)
  |     \-- Mesh com points animados no prim path correto
  |
  +-- [reference] asset.usd
        \-- Geometria estatica do asset
```

---

## 2. Plugin 1: USD Add Reference (Loader)

**Arquivo:** `load_maya_usd_add_reference.py`
**Classe:** `MayaUsdProxyReferenceUsd`

### Finalidade

Adicionar uma referencia USD (asset publicado) dentro de um stage USD
ja existente no Maya, em um `mayaUsdProxyShape`. Esse e o ponto de
entrada para trazer assets publicados para dentro de um shot USD.

### Product Types Suportados

`model`, `usd`, `rig`, `pointcache`, `animation`

### Representacoes Suportadas

`usd`, `usda`, `usdc`, `usdz`, `abc`

### Estrategias de Prim Path

O loader oferece 5 modos para determinar onde o prim sera criado no
stage:

| Modo             | Exemplo de Path                            | Descricao                          |
|------------------|--------------------------------------------|------------------------------------|
| Folder Path      | `/assets/character/cone_character`         | Estrutura de pastas completa       |
| Flat             | `/cone_character`                          | Apenas o nome do asset             |
| By Folder Type   | `/character/cone_character`                | Tipo extraido do path + nome       |
| Folder + Product | `/assets/character/cone_character/usdMain` | Path completo + nome do product    |
| Custom           | `/{folder_type}/{name}`                    | Variaveis expandiveis pelo usuario |

**Variaveis disponiveis no modo Custom:**
- `{name}` - nome do asset
- `{folder_name}` - nome da pasta
- `{folder_path}` - path completo da pasta
- `{folder_type}` - tipo da pasta pai (character, prop, etc.)
- `{product_name}` - nome do product (usdMain, etc.)
- `{parent_folder}` - pasta pai

**Custom paths absolutos vs relativos:**
- Absoluto (comeca com `/`): ignora prim selecionado, usa path direto
- Relativo (sem `/`): concatena ao prim atualmente selecionado

### Fluxo de Selecao de Stage (Prioridade)

```
1. USD Prim selecionado via UFE? -> usa como base
2. mayaUsdProxyShape selecionado? -> pega o stage dele
3. Existe algum proxy na cena?    -> usa o primeiro encontrado
4. Nenhum encontrado?             -> cria um novo proxy shape
```

### Fluxo de Load

```python
# 1. Determinar stage (prioridade acima)
stage = ...

# 2. Resolver prim path baseado no modo escolhido
prim_path = _resolve_prim_path(context, options)

# 3. Criar hierarquia de prims pai (Xform)
_define_prim_hierarchy(stage, prim_path)

# 4. Adicionar USD reference
reference = Sdf.Reference(assetPath=path, customData=identifier_data)
prim.GetReferences().AddReference(reference)

# 5. Containerizar o prim (metadata AYON)
containerise_prim(prim, name, namespace, context, loader)
```

### Metadata de Container (customData no prim)

Quando uma referencia e adicionada, os seguintes dados sao escritos no
prim como `customData`:

```python
{
    "ayon:schema": "openpype:container-2.0",
    "ayon:id": AVALON_CONTAINER_ID,
    "ayon:name": "cone_character",
    "ayon:namespace": "cone_character_01",
    "ayon:loader": "MayaUsdProxyReferenceUsd",
    "ayon:representation": "<representation_id>",
}
```

Esses dados sao fundamentais para:
- O Scene Manager do AYON rastrear versoes
- O collector do animation cache detectar o `originalAssetPrimPath`
  automaticamente
- Update/Switch/Remove de assets no stage

### Update / Switch / Remove

- **Update**: Itera pelo prim stack, encontra a referencia antiga e
  substitui o `assetPath` mantendo `customData`, `layerOffset` e `primPath`
- **Switch**: Chama `update()` diretamente
- **Remove**: Remove todas as references do prim e limpa customData

---

## 3. Plugin 2: Maya USD Rig (Creator + Extractor)

### Finalidade

Permitir o workflow de rigging dentro do USD: o rigger trabalha em Maya
nativo, e o rig e publicado como um `.mb` (Maya Binary) + um
`MayaReference` prim no USD que aponta para esse `.mb`. Quando o
animador clica "Edit as Maya Data" no prim, o Maya carrega o `.mb`
como dados nativos.

### 3.1 Creator (`create_maya_usd_rig.py`)

**Classe:** `CreateMayaUsdRig`
**Identifier:** `io.ayon.creators.maya.mayausdrig`
**Families:** `["rig", "usd", "mayaUsdRig"]`

**Pre-requisitos validados na criacao:**
1. `mayaUsdPlugin` carregado
2. `mayaUsdProxyShape` existe na cena
3. Edit target layer definida no Maya USD Layer Editor

**Sets criados automaticamente:**

```
{product_name}_controls_SET   # Controles do rig
{product_name}_skeleton_SET   # Joints/skeleton
{product_name}_geo_SET        # Geometria
```

**Atributos de instancia:**
- `includeGuides` (bool) - incluir curves guia
- `preserveReferences` (bool) - manter references no .mb
- `rigSuffix` (string) - sufixo para nodes de rig

### 3.2 Collector (`collect_maya_usd_rig.py`)

**Classe:** `CollectMayaUsdRig`
**Order:** `CollectorOrder + 0.1`

**Dados coletados:**

| Key                    | Descricao                            |
|------------------------|--------------------------------------|
| `usdProxyShape`        | Path do proxy shape                  |
| `usdStageProxyPath`    | Path do proxy (para mayaUsd API)     |
| `usdEditTargetLayer`   | Layer alvo para edicao               |
| `setMembers`           | Nodes do rig a exportar              |
| `rigMembers`           | Alias de setMembers                  |

**Validacoes:**
- `mayaUsdProxyShape` deve existir
- Stage deve ser acessivel via `mayaUsd.ufe.getStage()`
- Edit target layer deve estar definida

### 3.3 Extractor (`extract_maya_usd_rig.py`)

**Classe:** `ExtractMayaUsdRig`

**Fluxo de publicacao (6 etapas):**

```
1. Exportar rig como .mb (Maya Binary)
   - cmds.file(..., exportSelected=True, type="mayaBinary")
   - preserveReferences=False
   - Inclui: channels, constraints, expressions, constructionHistory

2. Calcular path absoluto do .mb publicado
   - publishDir + filename -> path absoluto
   - Necessario para "Edit as Maya Data" resolver corretamente

3. Criar MayaReference prim no USD
   - Abordagem 1: mayaUsdAddMayaReference.createMayaReferencePrim()
   - Abordagem 2 (fallback): stage.DefinePrim(path, "MayaReference")

4. Garantir que rigging layer existe
   - Busca layer com "rig" ou "rigging" no nome
   - Se nao encontrar, cria rigging.usda como sublayer

5. Exportar rigging layer como .usda
   - edit_layer.Export(filepath, args={"format": "usda"})

6. Transferir .mb para publishDir
   - Mesmo padrao de OBJ .mtl files
   - instance.data["transfers"].append((src, dst))
```

**MayaReference Prim - Atributos:**

```python
prim = stage.DefinePrim(prim_path, "MayaReference")

# Atributos criados:
prim.CreateAttribute("mayaReference", Sdf.ValueTypeNames.Asset)
# -> path absoluto do .mb publicado

prim.CreateAttribute("mayaNamespace", Sdf.ValueTypeNames.String)
# -> namespace para o reference (instance name)

prim.CreateAttribute("mayaAutoEdit", Sdf.ValueTypeNames.Bool)
# -> False (usuario ativa "Edit as Maya Data" manualmente)
```

**Busca de prim pai para o MayaReference:**

```
1. Default prim do stage -> usa como pai
2. Primeiro Xform/Scope na raiz -> usa como pai
3. Pseudo-root -> cria na raiz do stage
```

**Representations publicadas:**

| Nome | Ext  | Conteudo                                    |
|------|------|---------------------------------------------|
| `mb` | .mb  | Rig Maya (geometria, controles, skeleton)   |
| `usd`| .usd | Rigging layer com MayaReference prim        |

---

## 4. Plugin 3: Animation Cache USD / LayCache (Creator + Extractor)

### Finalidade

Exportar a geometria animada (deformada pelo rig) como um point cache
USD. Esse cache entra como sublayer (LayCache) no shot, sobrescrevendo
os pontos da geometria estatica do asset original.

### 4.1 Creator (`create_animation_cache_usd.py`)

**Classe:** `CreateAnimationCacheUsd`
**Identifier:** `io.ayon.creators.maya.animationcacheusd`
**Families:** `["animationCacheUsd", "usd"]`

**Atributos definidos:**

| Atributo               | Tipo   | Default   | Descricao                                |
|------------------------|--------|-----------|------------------------------------------|
| Frame range            | Number | Da cena   | Start/end/handles (via collect_animation_defs) |
| `animationSampling`    | Enum   | sparse    | sparse / per_frame / custom              |
| `customStepSize`       | Number | 1.0       | Step size quando sampling = custom       |
| `department`           | Enum   | auto      | auto / animation / layout / cfx / fx     |
| `originalAssetPrimPath`| Text   | ""        | Path do prim original (auto-detectado)   |
| `defaultUSDFormat`     | Enum   | usda      | usdc (binary) / usda (ASCII)             |
| `stripNamespaces`      | Bool   | True      | Remover namespaces do Maya               |

### 4.2 Collector (`collect_animation_cache_usd.py`)

**Classe:** `CollectAnimationCacheUsd`
**Order:** `CollectorOrder + 0.5`

#### Deteccao Automatica do Asset Prim Path

Essa e a parte mais critica do collector. O `originalAssetPrimPath`
determina onde a geometria do cache sera posicionada na composicao USD.

**Estrategia de deteccao (prioridade):**

```
1. Manual: creator_attributes["originalAssetPrimPath"]
   -> Se o usuario preencheu, usa diretamente.

2. Containers: match por namespace dos setMembers
   -> Busca todos mayaUsdProxyShape na cena
   -> Para cada proxy, pega o stage via mayaUsd.ufe.getStage()
   -> Traversa TODOS os prims procurando ayon:id == AVALON_CONTAINER_ID
   -> Se encontra 1 container, usa diretamente
   -> Se encontra N containers, faz match por namespace:
      - Extrai namespaces dos setMembers (ex: "myNs:pCube1" -> "myNs")
      - Compara com ayon:namespace e ayon:name dos containers
   -> Fallback: usa o primeiro container encontrado

3. UFE Selection: prim USD selecionado
   -> Itera iter_ufe_usd_selection()
   -> Extrai prim path do UFE path (formato: "nodeName,primPath")
```

#### Deteccao de Department

```python
task_name = task_entity["name"].lower()
# "anim" ou "animation" -> "animation"
# "layout"              -> "layout"
# "cfx"                 -> "cfx"
# "fx"                  -> "fx"
# outro                 -> "auto"
```

#### Dados armazenados na instance:

| Key                    | Descricao                                |
|------------------------|------------------------------------------|
| `originalAssetPrimPath`| Path do prim original no stage           |
| `departmentLayer`      | Department detectado                     |
| `samplingMode`         | sparse / per_frame / custom              |
| `customStepSize`       | Step size para sampling custom           |

### 4.3 Validators (`validate_animation_cache_usd.py`)

| Validator                     | Descricao                                 |
|-------------------------------|-------------------------------------------|
| `ValidateAnimatedMembersExist`| setMembers nao vazio + nodes existem      |
| `ValidateAssetPrimPathResolved`| originalAssetPrimPath detectado (warning) |
| `ValidateFrameRange`          | frameStart < frameEnd, stepSize > 0       |

### 4.4 Extractor (`extract_animation_cache_usd.py`)

**Classe:** `ExtractAnimationCacheUsd`

#### Fluxo de Publicacao

```
1. _export_animation_cache()
   - cmds.mayaUSDExport(**options) com selection=True
   - Gera USD com hierarquia do Maya (quebrada)

2. _remap_to_asset_hierarchy()
   - Encontra o prim do asset na hierarquia exportada
   - Cria nova layer com hierarquia correta
   - Copia geometria via Sdf.CopySpec()
   - Limpa prims nao-geometria

3. Adiciona representacao "usd"
```

#### Opcoes de Export

```python
options = {
    "file": filepath,
    "selection": True,              # APENAS nodes selecionados
    "frameRange": (start, end),
    "frameStride": frame_step,      # 1.0 padrao, customizavel
    "exportSkels": "none",          # Sem skeleton
    "exportSkin": "none",           # Sem skin clusters
    "exportBlendShapes": True,      # Blend shapes se existirem
    "stripNamespaces": True,        # Remove namespaces Maya
    "mergeTransformAndShape": False, # Mantem transform e shape separados
    "exportDisplayColor": False,
    "exportVisibility": False,
    "exportColorSets": False,
    "exportUVs": True,              # Mantem UVs para texturing
    "exportInstances": False,
    "defaultUSDFormat": "usdc",     # Binario compactado
    "staticSingleSample": False,    # Mantem keyframes de animacao
    "eulerFilter": True,
    "worldspace": True,             # Maya USD >= 0.21.0
}
```

#### Remapeamento de Hierarquia (Detalhe Tecnico)

Este e o core fix que resolve o problema da hierarquia quebrada.

**Problema:**

```
# O que o Maya exporta (hierarquia interna do Maya):
/__mayaUsd__/rigParent/rig/cone_character/geo/cone_character_GEOShape

# O que precisamos (para a sublayer compor corretamente):
/usdShot/assets/character/cone_character/geo/cone_character_GEOShape
```

**Algoritmo (`_remap_to_asset_hierarchy`):**

```python
def _remap_to_asset_hierarchy(self, filepath, instance):
    # 1. Obter o target path do collector
    original_path = instance.data["originalAssetPrimPath"]
    # Ex: "/usdShot/assets/character/cone_character"
    target_path = Sdf.Path(original_path)
    asset_name = target_path.name  # "cone_character"

    # 2. Abrir layer exportada
    layer = Sdf.Layer.FindOrOpen(filepath)

    # 3. Encontrar o prim do asset na hierarquia exportada
    source_path = _find_prim_by_name(layer, asset_name)
    # Encontra: /__mayaUsd__/rigParent/rig/cone_character

    # 4. Criar nova layer com hierarquia correta
    new_layer = Sdf.Layer.CreateAnonymous()
    _copy_layer_metadata(layer, new_layer)  # timeCode, upAxis, etc.

    # 5. Criar prims pai no target path
    # /usdShot -> Xform
    # /usdShot/assets -> Xform
    # /usdShot/assets/character -> Xform
    for prefix in target_path.GetPrefixes()[:-1]:
        prim_spec = Sdf.CreatePrimInLayer(new_layer, prefix)
        prim_spec.specifier = Sdf.SpecifierDef
        prim_spec.typeName = "Xform"

    # 6. Copiar subtree do asset inteiro
    Sdf.CopySpec(layer, source_path, new_layer, target_path)

    # 7. Limpar prims nao-geometria
    _cleanup_non_geometry(new_layer, target_path)

    # 8. Salvar
    new_layer.Export(filepath)
```

**Busca por nome do asset:**

```python
# Busca exata: procura prim com nome == asset_name
_find_prim_by_name(layer, "cone_character")
# -> /__mayaUsd__/rigParent/rig/cone_character

# Fallback com namespace: quando stripNamespaces=False
_find_prim_by_name_suffix(layer, "cone_character")
# -> Encontra "myNs:cone_character" pelo sufixo ":cone_character"
```

#### Limpeza de Prims Nao-Geometria

Apos o remapeamento, a layer e limpa em 2 passes:

**Pass 1 - Remover tipos nao-geometria:**

| Tipo Removido   | Razao                                    |
|------------------|------------------------------------------|
| `BasisCurves`    | Shapes de controles do rig               |
| `Material`       | Materiais pertencem ao look layer        |
| `Shader`         | Parte do sistema de materiais            |
| `NodeGraph`      | Parte do sistema de materiais            |
| `MayaReference`  | Referencia ao .mb (ja resolvida)         |

**Pass 2 - Remover containers vazios:**
- Xform/Scope que nao tenham descendentes de geometria
- Ex: scope `rig/` com todos os controles removidos -> removido tambem
- Tipos de geometria mantidos: `Mesh`, `GeomSubset`, `Points`,
  `NurbsPatch`, `PointInstancer`

**Exemplo do resultado:**

```
# ANTES (exportado pelo Maya):
/__mayaUsd__ (Xform)
  /rigParent (Xform)
    /rig (MayaReference)
      /cone_character (Xform)
        /geo (Scope)
          /cone_character_GEO (Xform)
            /cone_character_GEOShape (Mesh)  <- geometria animada
              /bottom (GeomSubset)
              /sides (GeomSubset)
        /rig (Xform)
          /Controls_Grp (Xform)
            /Main_Ctrl_Grp (Xform)
              /Main_Ctrl (Xform)
                /Main_CtrlShape (BasisCurves)  <- controle
                /Center_pivot_Ctrl_Grp (Xform)
                  /Center_pivot_Ctrl (Xform)
                    /Center_pivot_CtrlShape (BasisCurves)  <- controle
          /Deformer_Grp (Xform)  <- vazio

# DEPOIS (remapeado e limpo):
/usdShot (Xform)
  /assets (Xform)
    /character (Xform)
      /cone_character (Xform)
        /geo (Scope)
          /cone_character_GEO (Xform)
            /cone_character_GEOShape (Mesh)  <- geometria animada
              /bottom (GeomSubset)
              /sides (GeomSubset)
```

---

## 5. Composicao USD: Como as Layers se Encaixam

### Modelo de Composicao LIVRPS

O USD usa a regra LIVRPS para resolver opinioes:
**L**ocal > **I**nherits > **V**ariants > **R**eferences >
**P**ayloads > **S**pecializes

Para o nosso pipeline, as sublayers sao o mecanismo principal.
Sublayers sao resolvidas por **ordem de empilhamento** - a layer
mais forte (primeiro na lista) vence.

### Estrutura de Layers do Shot

```
shot_root.usda                    [Root Layer]
  |
  +-- subLayers:
  |     |
  |     +-- usdLayCache.usd       [Point Cache - MAIS FORTE]
  |     |     Contem: Mesh com points.timeSamples animados
  |     |     Path: /usdShot/assets/character/cone_character/geo/...
  |     |
  |     +-- rigging.usda          [Rigging Layer]
  |           Contem: MayaReference prim -> rig.mb
  |           Path: /cone_character/rig (MayaReference)
  |
  +-- references:
        +-- asset.usd             [Asset Original]
              Contem: Geometria estatica, materiais, lookdev
```

### Como o Point Cache Override Funciona

```
# Asset original (em asset.usd via reference):
def Mesh "cone_character_GEOShape" {
    point3f[] points = [(1, 0, 0), (0, 1, 0), ...]  # estatico
}

# Point cache (em usdLayCache.usd via sublayer):
def Mesh "cone_character_GEOShape" {
    point3f[] points.timeSamples = {
        1001: [(1.1, 0.2, 0.1), (0.1, 1.2, 0.3), ...],
        1002: [(1.2, 0.4, 0.2), (0.2, 1.3, 0.5), ...],
        ...
    }
}

# Resultado composto (sublayer vence sobre reference):
# -> Os points animados do cache substituem os estaticos do asset
# -> UVs, materiais, etc. vem do asset original
# -> Animacao fluida com dados minimos
```

---

## 6. Fluxo Completo: Do Asset ao Shot Animado

### Fase 1: Modelagem e Lookdev (Upstream)

```
1. Modelar asset no Maya
2. Publicar como USD (model/usd product type)
   -> Resultado: asset.usd com geometria estatica
3. Aplicar materiais/lookdev
   -> Resultado: look.usd como layer adicional
```

### Fase 2: Rigging

```
1. Abrir asset USD no Maya via "Load Maya USD" (proxy shape)
2. Definir Edit Target Layer no Maya USD Layer Editor
3. Criar instancia "Maya USD: Rig" (io.ayon.creators.maya.mayausdrig)
   -> Valida: proxy existe, edit target definido
   -> Cria sets: _controls_SET, _skeleton_SET, _geo_SET
4. Construir o rig no Maya (joints, controles, skinning)
5. Popular os sets com os nodes do rig
6. Publicar:
   a. Exporta rig como .mb (Maya Binary)
   b. Cria MayaReference prim no USD -> aponta para .mb
   c. Exporta rigging layer (.usda)
   d. Transfere .mb para publishDir
   -> Resultado: rigging.usda + rig.mb
```

### Fase 3: Layout / Animacao (Shot)

```
1. Abrir shot USD no Maya
2. Carregar asset no shot:
   - Right-click no asset -> "USD Add Reference"
   - Escolher modo de prim path (Folder Path recomendado)
   -> Asset aparece no stage com metadata AYON

3. Ativar "Edit as Maya Data" no prim /rig (MayaReference)
   -> Maya carrega o .mb como dados nativos
   -> Geometria e controles ficam editaveis no Maya
   -> Nodes recebem namespace (ex: "cone_character:")

4. Esconder geometria USD original (opcional)
   -> Para evitar dupla visualizacao

5. Animar o rig no Maya
   -> Keyframes nos controles
   -> Geometria deforma com a animacao
```

### Fase 4: Exportar Point Cache (LayCache)

```
1. Selecionar a geometria animada no Maya
   (ex: cone_character:cone_character_GEOShape)

2. Criar instancia "Animation Cache USD"
   (io.ayon.creators.maya.animationcacheusd)
   - Verificar: animationSampling, frame range, department
   - originalAssetPrimPath: AUTO-DETECTADO (nao precisa preencher)

3. Publicar:
   a. Collector detecta:
      - originalAssetPrimPath via containers AYON no stage
      - Department via task context
      - Sampling settings
   b. Validators verificam:
      - Members existem na cena
      - Frame range valido
   c. Extractor:
      - Exporta USD com cmds.mayaUSDExport(selection=True, ...)
      - Pos-processa: remapeia hierarquia para prim path correto
      - Limpa prims nao-geometria (controles, materiais)
   -> Resultado: usdLayCache.usd com geometria no path correto

4. Integrar no shot:
   - usdLayCache.usd entra como sublayer no shot root
   - Points animados sobrescrevem os estaticos do asset
   - Composicao USD resolve automaticamente
```

---

## 7. API e Utilitarios

### usdlib.py

```python
# Containerizar um prim com metadata AYON
containerise_prim(prim, name, namespace, context, loader)
# -> Escreve ayon:id, ayon:name, ayon:namespace, etc.

# Iterar selecao UFE de prims USD
for ufe_path in iter_ufe_usd_selection():
    node, prim_path = ufe_path.split(",", 1)

# Remover spec de uma layer
remove_spec(spec)  # PrimSpec ou PropertySpec
```

### Export Filter Chaser

```python
# Filtra properties durante export USD
# Usa patterns estilo Houdini: *, ?, [abc], ^ para excluir
class FilterPropertiesExportChaser(mayaUsdLib.ExportChaser):
    def PostExport(self):
        # Remove property specs que nao matcham o pattern
```

### Funcoes de Hierarquia (load_maya_usd_add_reference.py)

```python
# Sanitizar nomes para USD (remover caracteres invalidos)
_sanitize(name)  # "my-name.1" -> "my_name_1"

# Criar hierarquia de prims pai
_define_prim_hierarchy(stage, "/assets/character/cone_character")
# -> Cria: /assets (Xform), /assets/character (Xform), ...

# Resolver prim path baseado em modo + contexto
_resolve_prim_path(mode, context, options, base_prim)
```

---

## 8. Troubleshooting e Edge Cases

### "originalAssetPrimPath nao detectado"

**Causa:** Nenhum container AYON encontrado no stage USD.

**Solucoes:**
1. Verificar que o asset foi carregado via "USD Add Reference" (que
   escreve metadata ayon:id no prim)
2. Preencher manualmente no creator: campo "Original Asset Prim Path"
3. Selecionar o prim do asset no viewport antes de publicar (fallback
   UFE)

### "Hierarquia do cache nao compoe sobre o asset"

**Causa:** O prim path no cache nao corresponde ao path do asset no
shot.

**Verificacao:**
1. Abrir o USD publicado em um editor de texto
2. Verificar que o prim path da Mesh esta identico ao do asset no shot
3. Verificar que `originalAssetPrimPath` foi detectado corretamente
   (ver log do collector)

### "Controles do rig aparecem no cache"

**Causa:** A limpeza de prims nao-geometria pode ter falhado.

**Verificacao:**
1. Verificar que BasisCurves foram removidos (ver log do extractor)
2. Se o tipo do prim de controle nao esta na lista de tipos removidos,
   adicionar em `non_geo_types` no `_cleanup_non_geometry()`

### "Namespaces no USD exportado"

**Causa:** `stripNamespaces=False` no creator.

**Comportamento:**
- Se True (padrao): namespaces sao removidos, match por nome funciona
- Se False: o fallback `_find_prim_by_name_suffix()` busca por sufixo
  `:asset_name`

### "Edit as Maya Data nao carrega o .mb"

**Causa:** Path do .mb no MayaReference prim esta incorreto.

**Verificacao:**
1. Inspecionar o atributo `mayaReference` no prim
2. Deve ser um path absoluto para o .mb publicado
3. Verificar que o transfer do .mb para publishDir funcionou
4. Verificar que o path e acessivel na maquina atual

### "MayaUSD version < 0.21.0"

**Impacto:** Opcao `worldspace=True` nao sera usada no export.

**Solucao:** Atualizar MayaUSD plugin ou Maya para versao 2025.3+.
O export ainda funciona, mas coordenadas serao em local space em vez
de world space.

---

## Apendice: Mapa de Dependencias entre Plugins

```
create_animation_cache_usd.py
  |
  v
collect_animation_cache_usd.py  -->  usdlib.py (containers, UFE)
  |                                     |
  v                                     v
validate_animation_cache_usd.py    mayaUsd.ufe (API nativa)
  |
  v
extract_animation_cache_usd.py  -->  pxr.Sdf (remapeamento)
  |
  v
[LayCache USD publicado] --> sublayer no shot root

create_maya_usd_rig.py
  |
  v
collect_maya_usd_rig.py  -->  mayaUsd.ufe (stage access)
  |
  v
extract_maya_usd_rig.py  -->  mayaUsdAddMayaReference (API)
  |                             pxr.Sdf (layer management)
  v
[rigging.usda + rig.mb publicados]

load_maya_usd_add_reference.py  -->  usdlib.py (containerise)
  |                                    pxr.Usd, Sdf (stage/ref)
  v
[Asset referenciado no shot stage com metadata AYON]
```
