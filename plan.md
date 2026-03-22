# Plano: Integrar USD Add Reference com Scene Inventory do AYON

## Problema

O loader `MayaUsdProxyReferenceUsd` (`load_maya_usd_add_reference.py`) usa
`containerise_prim()` que armazena metadados em `customData` de prims USD.
Porem, o Scene Inventory do AYON usa `ls()` (`pipeline.py:453`) que so
itera Maya objectSets (`kSet`). Resultado: assets USD sao **invisiveis** no
Scene Inventory, sem versionamento, sem notificacoes de desatualizados.

## Solucao: Container Maya "Shadow"

Criar um objectSet Maya que "espelha" o container USD, armazenando o prim
path como referencia. O Scene Inventory encontra o objectSet normalmente e
os metodos `update()`/`remove()` operam em ambos (objectSet + prim USD).

Essa abordagem:
- Nao modifica o core do AYON (`_ls()`, `ls()`, `parse_container()`)
- Reutiliza `containerise()` de `pipeline.py`
- Segue o mesmo padrao do `MayaUsdLoader` (`load_maya_usd.py`) que ja funciona
- Mantem compatibilidade com `containerise_prim()` (prim customData)

## Arquivos a Modificar

### 1. `client/ayon_maya/plugins/load/load_maya_usd_add_reference.py`

Modificacoes no loader principal:

#### 1.1 `load()` - Adicionar container Maya

Apos o `containerise_prim()` existente, adicionar:

```python
# Criar um objectSet container Maya para tracking no Scene Inventory
from ayon_maya.api.pipeline import containerise

# Usar o proxy shape como membro do container
# (precisamos de pelo menos um node Maya para o objectSet)
proxy_shapes = cmds.ls(type="mayaUsdProxyShape", long=True)
proxy_shape = None
for shape in proxy_shapes:
    s = _get_stage_from_proxy_shape(shape)
    if s and s == stage:
        proxy_shape = shape
        break

if proxy_shape:
    namespace = namespace or name
    container_node = containerise(
        name=name,
        namespace=namespace,
        nodes=[proxy_shape],
        context=context,
        loader=self.__class__.__name__
    )

    # Armazenar prim path e stage info no container para recuperar depois
    cmds.addAttr(container_node, longName="usd_prim_path", dataType="string")
    cmds.setAttr(container_node + ".usd_prim_path",
                 str(prim.GetPath()), type="string")
    cmds.addAttr(container_node, longName="usd_proxy_shape", dataType="string")
    cmds.setAttr(container_node + ".usd_proxy_shape",
                 proxy_shape, type="string")
```

#### 1.2 `update()` - Atualizar container Maya + USD prim

```python
def update(self, container, context):
    from pxr import Sdf

    # 1. Recuperar o prim USD a partir do container Maya
    node = container["objectName"]
    prim_path = cmds.getAttr(node + ".usd_prim_path")
    proxy_shape = cmds.getAttr(node + ".usd_proxy_shape")
    stage = _get_stage_from_proxy_shape(proxy_shape)
    prim = stage.GetPrimAtPath(prim_path)

    # 2. Atualizar a referencia USD (logica existente)
    path = self.filepath_from_context(context)
    for references, index in self._get_prim_references(prim):
        reference = references[index]
        new_reference = Sdf.Reference(
            assetPath=path,
            customData=reference.customData,
            layerOffset=reference.layerOffset,
            primPath=reference.primPath
        )
        references[index] = new_reference

    # 3. Atualizar customData do prim
    prim.SetCustomDataByKey(
        "ayon:representation", context["representation"]["id"]
    )

    # 4. Atualizar o container Maya (representation ID)
    cmds.setAttr(node + ".representation",
                 context["representation"]["id"], type="string")
```

#### 1.3 `switch()` - Delegar para update

```python
def switch(self, container, context):
    self.update(container, context)
```

#### 1.4 `remove()` - Remover container Maya + USD prim

```python
def remove(self, container):
    node = container["objectName"]
    prim_path = cmds.getAttr(node + ".usd_prim_path")
    proxy_shape = cmds.getAttr(node + ".usd_proxy_shape")
    stage = _get_stage_from_proxy_shape(proxy_shape)

    if stage:
        prim = stage.GetPrimAtPath(prim_path)
        if prim and prim.IsValid():
            # Remover referencias USD
            related_refs = reversed(list(self._get_prim_references(prim)))
            for references, index in related_refs:
                references.remove(references[index])
            prim.ClearCustomDataByKey("ayon")

    # Remover o container Maya (objectSet)
    if cmds.objExists(node):
        cmds.delete(node)
```

### 2. `client/ayon_maya/api/usdlib.py` (Opcional - manter compatibilidade)

Manter `containerise_prim()` como esta - continua sendo util para armazenar
metadados no prim USD. O container Maya e o prim USD coexistem.

### 3. `docs/USD_PIPELINE_TECHNICAL_GUIDE.md`

Atualizar a documentacao para refletir:
- Nova integracao com Scene Inventory
- Fluxo de versionamento e notificacoes
- Novos atributos do container (`usd_prim_path`, `usd_proxy_shape`)

## Detalhes de Implementacao

### Fluxo Completo (load)

1. Usuario seleciona asset no Loader e escolhe "USD Add Reference"
2. Loader resolve o prim path e adiciona referencia USD ao stage
3. `containerise_prim()` armazena metadados no prim (mantido)
4. **NOVO**: `containerise()` cria objectSet Maya com proxy shape como membro
5. **NOVO**: Atributos extras (`usd_prim_path`, `usd_proxy_shape`) sao adicionados ao objectSet
6. Scene Inventory agora "ve" o container e pode gerencia-lo

### Fluxo Completo (update via Scene Inventory)

1. Scene Inventory detecta container desatualizado (via `any_outdated_containers()`)
2. Popup avisa o usuario
3. Usuario abre Scene Inventory e clica "Update"
4. `update()` recebe o container Maya, recupera prim path e proxy shape
5. Atualiza referencia USD no stage
6. Atualiza `representation` no objectSet Maya

### Fluxo Completo (remove via Scene Inventory)

1. Usuario seleciona container no Scene Inventory e clica "Remove"
2. `remove()` recebe o container Maya, recupera prim path
3. Remove referencias USD do prim
4. Limpa customData do prim
5. Deleta objectSet Maya

## Riscos e Mitigacoes

| Risco | Mitigacao |
|-------|-----------|
| Proxy shape pode ser removido entre load e update | Validar existencia do proxy shape em update/remove |
| Prim pode ter sido deletado manualmente | Validar prim antes de operar |
| Multiplos assets no mesmo prim | Usar identifier_key existente para diferenciar |
| Container Maya fica orfao se prim for removido fora do AYON | Adicionar validacao no update() |

## Ordem de Implementacao

1. Modificar `load()` para criar container Maya shadow
2. Reescrever `update()` para usar container Maya como entrada
3. Reescrever `remove()` para limpar ambos
4. Adicionar tratamento de erros (proxy shape removido, prim invalido)
5. Testar integracao com Scene Inventory
6. Atualizar documentacao
