# Investimentos — carteira e projeções

Aplicativo web em português para acompanhar investimentos de várias classes em uma carteira compartilhada. Reaproveita as fórmulas úteis da planilha de referência, mas roda sem Excel, VBA ou dependência do arquivo original.

## Cadastro e importação

- Cadastre manualmente títulos públicos e privados, fundos, ações, FIIs, ETFs, BDRs, criptoativos, ouro, COEs, opções, previdência e outras posições.
- O botão **Produto** abre a lista de produtos existente. Ao selecionar um, o app abre o cadastro de aporte; para um produto novo, informe apenas corretora, tipo e nome e então registre o aporte.
- CDBs, LCIs e LCAs têm tipos próprios. Ao cadastrar uma posição, informe também a data do primeiro aporte; os valores e a quantidade podem ser preenchidos na tabela de aportes individuais.
- Cada investimento agrupa vários aportes individuais. Clique no nome da posição para trocar a lista da carteira pela tabela completa de aportes, adicionar linhas ou editar cada uma; use **Minha carteira** para voltar.
- Todas as tabelas permitem ordenar os dados clicando no cabeçalho das colunas.
- Clique no nome de uma corretora na carteira para abrir a tabela somente com os produtos dela, cálculos e totais recalculados para esse grupo. Nessa visão, **Adicionar produto** mostra somente produtos daquela corretora e fixa a corretora no cadastro de um novo produto.
- Cada aporte guarda quantidade, valor investido e atual, preço por unidade, datas de aporte, vencimento e referência, e observações sobre taxas. A corretora, categoria e nome ficam no investimento.
- A tabela de aportes calcula retorno, dias decorridos, taxa diária e projeções para cada linha. O rodapé soma quantidades e valores e aplica a taxa agregada usando a data média dos aportes com data, como a linha de totais da aba `modelo`. Projeções do próximo dia exigem 5 dias de histórico; as de 30 dias exigem 30; as de 360 dias exigem 90, para evitar extrapolar um retorno de um ou poucos dias. A carteira também apresenta totais consolidados e taxas mensais e anuais projetadas. Essas projeções não são o retorno efetivo dos últimos 12 meses: o app não armazena uma série histórica de cotações.
- Importe o PDF detalhado de fundos da Ágora. O app apresenta uma prévia e permite preencher ou corrigir a data de aporte antes de gravar cada linha como um aporte.
- As datas de aportes importados podem ser corrigidas na tabela de detalhes e são mantidas quando o mesmo extrato for reimportado.
- Reimporte um extrato para atualizar as linhas que o app reconhece como importadas da Ágora. As posições manuais não entram nessa sincronização e não são apagadas.
- Na tela **Atualizar valores**, cada linha corresponde a um aporte individual, com data e valor atual próprios. Salve uma linha isoladamente ou use **Salvar alterados** para gravar várias linhas; os valores não são agrupados nem redistribuídos entre os aportes do mesmo produto. O app também permite corrigir cada aporte na tabela de detalhes. Não acessa contas de corretoras nem atualiza automaticamente os preços dos investimentos.
- Ao criar um aporte, escolha BRL, USD ou BTC para os valores investido e atual. O investido é convertido uma vez no cadastro; depois fica fixo em reais. Ao editar um aporte, a moeda selecionada vale para o valor atual e o preço por unidade, que são convertidos ao salvar; o valor investido permanece em BRL. O preço por unidade é calculado pelo valor atual dividido pela quantidade, e a seleção de outra moeda converte os valores exibidos para preservar o equivalente. Na atualização em lote, a moeda escolhida se aplica ao valor atual. A interface mostra uma prévia em reais e a fonte/horário da taxa. USD usa a última cotação de venda PTAX disponível do Banco Central (boletim diário, podendo ser do último dia útil); BTC usa o preço spot BTC/BRL público da Coinbase. O servidor converte antes de gravar, e o banco continua guardando valores monetários em centavos de real; quantidades/unidades não são convertidas. Se a fonte de cotação estiver indisponível, a gravação em moeda estrangeira é recusada em vez de usar uma taxa presumida.
- Faça backup e restauração JSON pela interface. A carteira usa SQLite num volume persistente do Docker.

O extrato da Ágora informa a data de referência das cotações, que não é necessariamente a data de aporte. O importador mantém esses campos separados. Sem uma data confirmada, o aporte é guardado, mas sua taxa diária e projeções individuais ficam em branco. No resumo, a fórmula da planilha ignora datas vazias ao calcular a média; o app mantém esse comportamento. A importação só processa a tabela detalhada de fundos e clubes de investimentos, com texto selecionável, até 15 MB e 100 páginas.

## Cálculos

O app calcula retorno acumulado e taxa diária geométrica a partir do valor investido, valor atual e data de aporte. Títulos, fundos e demais classes usam dias úteis, excluindo fins de semana e feriados cadastrados. Criptoativos usam dias corridos. As projeções extrapolam a rentabilidade observada para o próximo dia e períodos de 30 e 360 dias. A linha principal da carteira mostra o resumo de cada investimento; ao abrir uma posição, a tabela apresenta cálculos por aporte e o total agregado. Carteiras com bases diferentes não recebem projeção agregada; cada posição continua sendo calculada individualmente.

As projeções são estimativas matemáticas, sem desconto de imposto ou taxas, e não representam promessa de rendimento. A lista inicial do calendário contém as 753 datas da aba `feriados` do arquivo de referência; ela não cobre necessariamente feriados municipais ou datas especiais. Ajuste o calendário no app conforme necessário.

## Acesso e usuários

O app inclui login, troca obrigatória da senha inicial e painel administrativo para criar, redefinir, ativar, desativar ou excluir contas. O administrador decide quem pode editar posições, calendário e backups. Todos os usuários autorizados veem a mesma carteira.

As senhas são armazenadas com hashes bcrypt. As sessões expiram após sete dias e podem ser revogadas por usuário; o cookie usa `HttpOnly` e `SameSite=Strict`. A imagem Docker roda sem privilégios e o sistema de arquivos do container é somente leitura, exceto o volume de dados.

## Ubuntu Server e CasaOS

Requer Docker Engine e Docker Compose v2. O `compose.yaml` inclui metadados para o app aparecer no painel CasaOS.

1. Copie esta pasta para `/DATA/AppData/titulos-investimentos` no Ubuntu Server.
2. Crie `app.env` a partir de `.env.example` e defina `INITIAL_ADMIN_USERNAME` e `INITIAL_ADMIN_PASSWORD`. Use uma senha de pelo menos 12 caracteres e no máximo 72 bytes.
3. Prepare o volume e construa a imagem:

   ```bash
   mkdir -p data
   sudo chown 10001:10001 data
   docker build -t local/titulos-investimentos:1.5.0 .
   ```

4. Registre o app no CasaOS:

   ```bash
   casaos-cli app-management install --file compose.yaml
   ```

O app aparece como **Investimentos** na porta `8082`. Entre com a conta inicial e troque a senha no primeiro acesso. Os dados ficam em `/DATA/AppData/titulos-investimentos/data`. O botão **Baixar backup** gera uma cópia JSON; a restauração substitui posições e calendário atuais.

A porta fica acessível na rede local do servidor. Para acesso remoto, use VPN ou proxy HTTPS; atrás de HTTPS, configure `COOKIE_SECURE=1` em `app.env`.

## Estrutura

```text
titulos-investimentos/
├── app/
│   ├── agora_import.py
│   ├── data_seed/holidays.json
│   ├── static/
│   └── main.py
├── compose.yaml
├── Dockerfile
├── .env.example
└── requirements.txt
```
