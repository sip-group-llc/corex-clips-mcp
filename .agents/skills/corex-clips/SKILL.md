---
name: corex-clips
description: Operar o CoreX Clips pelo MCP para ingerir vídeos, transcrever, selecionar cortes, revisar, renderizar e baixar arquivos.
---

# CoreX Clips pelo MCP

Endpoint: https://clips.corex11.tech/api/mcp.
[Cliente e instruções](https://github.com/sip-group-llc/corex-clips-mcp/blob/main/README.md).

O serviço hospedado executa ingestão, processamento e renderização. Conecte um cliente MCP HTTP usando sua chave pessoal configurada fora do repositório. Em Agentes e MCP, a conta autoriza a conexão e os escopos `read`, `edit`, `generate` e `render`.

## Fluxo

1. Leia o catálogo MCP completo, continuando por `nextCursor`. A conexão anuncia 52 ferramentas; o cliente local acrescenta `clips_upload_file`.
2. Consulte os projetos e selecione ou crie o destino pedido.
3. Use `clips_create_with_chat` com o link/vídeo, instrução e chave de idempotência. Acompanhe `clips_assistant_run`.
4. Leia transcrições por páginas; confira os trechos antes de decidir cortes.
5. Para ajustes, leia a revisão atual e `editVersion`, proponha a alteração e aplique somente o que foi solicitado.
6. Use `clips_render_revision` ou `clips_render`. Acompanhe o job e confira `clips_list_files`: `file: null` significa que o arquivo final ainda não existe.
7. Obtenha uma URL atual para baixar o resultado; URLs assinadas expiram. Prévia não é render final.

## Vídeo local

Instale o cliente Python 3.11+ do repositório, com `python -m pip install .`, e configure:
```text
corex-clips-mcp configure --upload-root CAMINHO_DA_PASTA_DE_VIDEOS
```
Servidor stdio: executável `corex-clips-mcp`, argumento `mcp`.
`clips_upload_file` recebe `file_path`, `projectId` e `confirmed: true` após autorização do envio/processamento. Suporta vídeos de até 15 GB e requer espaço para uma cópia temporária. Os bytes seguem ao armazenamento por URL assinada.

Guarde IDs e recibos. Após erro incerto, consulte vídeos/jobs antes de reenviar. Consumo segue os créditos e cotas da conta. Exclusão de mídia e publicação em redes sociais não são ferramentas deste MCP.
