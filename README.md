# MKV Tool

Ferramenta de linha de comandos para extrair e adicionar áudio, cortar ficheiros
MKV e sincronizar episódios. O workflow também pode processar pastas em bulk.

Os ficheiros de origem não são alterados; os resultados são gravados como novos
ficheiros.

## Requisitos

- Python 3.8 ou superior.
- `ffmpeg` e `ffprobe` acessíveis através do `PATH`.
- `mkvmerge` e `mkvinfo` no `PATH` quando se usam `--op1` e `--op2`.

Verifica a instalação no PowerShell:

```powershell
python --version
ffmpeg -version
ffprobe -version
mkvmerge --version
mkvinfo --version
```

`pytest` é necessário apenas para executar a suíte de testes.

## Comandos

```powershell
python .\new_mkv_tool.py --help
python .\new_mkv_tool.py workflow --help
```

### `extract` — extrair uma faixa de áudio

Extrai áudio sem recodificar. O índice de faixa começa em zero.

```powershell
python .\new_mkv_tool.py extract ".\episodio.mkv"
python .\new_mkv_tool.py extract ".\episodio.mkv" --lang por
```

### `trim` — cortar vídeo

O corte pode ser indicado por tempos ou por capítulos.

```powershell
python .\new_mkv_tool.py trim ".\episodio.mkv" --start-cut 1:49 --end-cut 2:13
python .\new_mkv_tool.py trim ".\episodio.mkv" --start-chapter 2 --end-chapter 5
```

### `add-audio` — adicionar uma faixa de áudio

O áudio pode ser um ficheiro ou uma pasta com ficheiros emparelhados pelo nome.

```powershell
python .\new_mkv_tool.py add-audio ".\episodio.mkv" --audio ".\audio.ac3" --lang por --title "Português"
```

### `detect-sync` — detetar um ponto de sincronização

Compara mudanças de plano no ficheiro 1 e no ficheiro 2 para encontrar onde o
conteúdo do primeiro começa no segundo.

```powershell
python .\new_mkv_tool.py detect-sync ".\audio_video.mkv" ".\episodio_com_abertura.mkv"
```

Por predefinição, a sensibilidade (`--threshold`) é `0.3`. Um valor menor deteta
mais cortes; usa outro valor apenas se a deteção predefinida não funcionar.
`--detect-length` controla quantos segundos do ficheiro 1 analisar e
`--detect-search` controla quanto procurar no ficheiro 2.

## Workflow com ficheiros individuais

O primeiro ficheiro fornece a faixa de áudio; o segundo fornece o vídeo.
Por predefinição, o programa conserva as faixas originais do vídeo e adiciona
uma faixa de áudio.

```powershell
python .\new_mkv_tool.py workflow ".\audio_video.mkv" ".\episodio.mkv" --auto-sync --lang por --title "Português" --default
```

Opções comuns:

| Opção | Comportamento |
|---|---|
| `--auto-sync` | Deteta automaticamente onde o áudio do primeiro ficheiro encaixa no vídeo. |
| `--start-cut`, `--end-cut` | Define limites por tempo; também podem ser usados capítulos. |
| `--no-restore` | Mantém apenas o segmento do episódio, sem repor o início e o fim removidos. |
| `--end-cut 2` | No modo `--no-restore`, remove mais 2 segundos depois do fim previsto do áudio. |
| `--post-sync-start-cut 0.30` | Depois do auto-sync, corta 0,30 s adicionais do início do vídeo e do áudio importado. Requer `--auto-sync --no-restore`. |
| `--offset SEGUNDOS` | Ajusta o posicionamento do áudio; um valor negativo adianta-o. |
| `--src-track N`, `--src-lang por` | Escolhe a faixa de áudio do primeiro ficheiro por índice ou idioma. |
| `--lang`, `--title` | Define o idioma e o título da faixa adicionada. |
| `--default` | Torna a faixa adicionada a faixa de áudio predefinida. |
| `--out PASTA` | Define a pasta de destino. Por predefinição, o resultado fica junto ao vídeo. |
| `--overwrite` | Permite substituir um resultado já existente. |

### Fonte de áudio AC3 já sincronizada

Usa `--audio-only` quando o primeiro ficheiro contém apenas áudio já sincronizado.
É necessário indicar os limites manuais e uma abertura para o início e o fim:

```powershell
python .\new_mkv_tool.py workflow ".\audio.ac3" ".\episodio.mkv" --audio-only --start-cut 1:49 --end-cut 2:08 --opening ".\abertura.mkv" --lang por
```

### Abertura no início e no fim

`--opening` monta a abertura antes e depois do corpo do episódio. O resultado
mantém apenas o vídeo e a faixa de áudio composta; outras faixas e legendas são
removidas.

```powershell
python .\new_mkv_tool.py workflow ".\audio_video.mkv" ".\episodio.mkv" --auto-sync --opening ".\abertura.mkv" --lang por --title "Português"
```

### Aberturas modulares

`--op1` e `--op2` usam Segment Linking e Ordered Chapters em vez de incorporar as
aberturas no vídeo. Esta opção requer MKVToolNix (`mkvmerge` e `mkvinfo`).

```powershell
python .\new_mkv_tool.py workflow ".\audio" ".\video" --auto-sync --op1 ".\abertura_original.mkv" --op2 ".\abertura_alternativa.mkv" --lang por
```

## Workflow em bulk

Indica duas pastas: a primeira com os MKVs de origem do áudio e a segunda com
os vídeos a cortar. Com `--auto-sync`, os ficheiros da primeira pasta também
têm de conter vídeo, pois a deteção compara mudanças de plano. Os MKVs de cada
pasta são ordenados alfabeticamente, sem distinção entre maiúsculas e
minúsculas, e emparelhados pela posição na lista. As pastas têm de conter o
mesmo número de MKVs.

Com `--auto-sync`, é detetado um timestamp no primeiro episódio de cada grupo
de 10. Esse timestamp é reutilizado nos nove episódios seguintes. O grupo
seguinte começa com uma nova deteção. Se a deteção do primeiro episódio de um
grupo falhar, os restantes episódios desse grupo são ignorados para evitar
aplicar uma sincronização incorreta.

```powershell
python .\new_mkv_tool.py workflow ".\pasta_audio" ".\pasta_video" --out ".\resultado" --auto-sync --no-restore --end-cut 2 --post-sync-start-cut 0.30 --lang por --title "Português" --default --start-episode 1 --limit-episodes 100
```

- `--start-episode N` começa na posição `N`, a contar de 1, na lista alfabética
  dos pares. Por exemplo, `--start-episode 21` começa no 21.º par.
- `--limit-episodes N` processa no máximo `N` episódios a partir dessa posição.
- O agrupamento de dez começa no episódio selecionado por `--start-episode`.
- `-r` / `--recursive` inclui MKVs em subpastas na lista ordenada.
- Antes de processar, o programa mostra os pares para confirmação. `--yes`
  dispensa essa confirmação; confirma primeiro se a ordenação corresponde aos
  pares pretendidos.
- Usa uma pasta `--out` separada das pastas de origem para manter os resultados
  afastados dos ficheiros de entrada.

Se uma deteção falhar, o episódio inicial do grupo e os episódios ignorados
aparecem no resumo final como erros.

## Testes

Com `pytest` instalado:

```powershell
python -m pytest -q
```
