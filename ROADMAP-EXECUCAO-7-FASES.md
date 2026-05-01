# Roadmap Formal de Execução em 7 Fases

## Objetivo
Estabelecer um plano formal, progressivo e tecnicamente coerente para transformar o sistema atual em um SaaS de cibersegurança automatizada por IA, com foco em:

1. redução de redundância e ruído no código atual
2. fortalecimento da base operacional e de dados
3. evolução controlada do produto principal
4. aumento de previsibilidade técnica e comercial
5. preparação estrutural para sistemas mais avançados
6. eficiência econômica no uso de IA e tokens
7. construção de um produto vendável, escalável e rentável

## Escopo do Documento
Este documento organiza a execução em **7 fases principais**, cada uma contendo **7 subdivisões de alterações sistêmicas**. A ordem foi definida do nível mais básico e corretivo até os sistemas mais avançados, de modo que cada fase prepare corretamente a seguinte.

## Princípios de Execução
1. Não construir sistemas avançados sobre base instável.
2. Não usar IA onde lógica determinística resolve melhor e mais barato.
3. Não ampliar escopo antes de consolidar fluxo, dados e produto principal.
4. Não esconder valor em áreas administrativas se o usuário comum precisa enxergá-lo.
5. Não tratar interface como maquiagem; a UI deve refletir o modelo operacional.
6. Não misturar backlog técnico com diferenciais comerciais sem priorização.
7. Não otimizar escala antes de garantir utilidade, margem e retenção.

---

## Fase 1. Saneamento Estrutural do Sistema
### Objetivo
Reduzir redundância, ambiguidade e fragilidade no código atual, preparando uma base limpa para crescimento controlado.

### 7 subdivisões de alteração
#### 1.1 Consolidar responsabilidades entre rotas, services e worker
Separar com mais rigor:
- camada HTTP
- camada de orquestração
- camada de processamento
- camada de persistência lógica

Eliminar trechos em que a mesma regra aparece em mais de um ponto.

#### 1.2 Padronizar nomenclatura de conceitos centrais
Uniformizar termos como:
- audit
- monitoring run
- finding
- priority
- provider
- site
- resource
- dossier

Evitar nomes múltiplos para a mesma entidade lógica.

#### 1.3 Eliminar duplicações de fluxo e fallback espalhados
Centralizar:
- resolução de provider
- leitura de configuração por org
- fallback de modelos
- comportamento em erro
- mensagens de estado

#### 1.4 Revisar e simplificar migrations
Transformar migrations frágeis em migrations tolerantes a ambiente parcialmente migrado.

Padronizar:
- revisão
- dependências
- idempotência mínima
- compatibilidade com deploy incremental

#### 1.5 Criar mapa técnico das dependências reais do produto
Documentar:
- quais módulos são usados de fato
- quais são auxiliares
- quais são promessas futuras ainda não sustentadas

#### 1.6 Reduzir complexidade exposta na UI sem remover capacidade interna
Manter a potência técnica, mas esconder:
- campos prematuros
- controles raros
- inputs de baixo valor

#### 1.7 Definir critérios formais de “pronto para construir em cima”
A Fase 1 só termina quando existir:
- fluxo previsível
- nomenclatura consistente
- configuração centralizada
- migrations estáveis
- UI básica sem ruído excessivo

### Critério de saída
O sistema deve estar tecnicamente compreensível, com menos duplicação e menor risco de regressão estrutural.

---

## Fase 2. Base Operacional e de Configuração
### Objetivo
Transformar a configuração essencial do SaaS em uma base operacional simples, validável e reutilizável por todo o sistema.

### 7 subdivisões de alteração
#### 2.1 Formalizar a camada de configuração essencial
Estabelecer como base:
- provider de IA
- chave
- modelo
- recurso principal
- frequência
- estado de prontidão

#### 2.2 Criar estado formal de readiness do sistema
O sistema deve saber informar claramente:
- configurado
- parcialmente configurado
- pronto para executar
- bloqueado por erro

#### 2.3 Simplificar o onboarding operacional
Transformar setup em um caminho curto:
1. conectar provedor
2. validar
3. cadastrar recurso principal
4. executar primeira análise

#### 2.4 Centralizar validação preventiva
Validar antes de executar:
- provider
- modelo
- credencial
- base URL
- compatibilidade mínima

#### 2.5 Reduzir dependência de configuração manual repetida
O que for salvo na base deve abastecer:
- auditoria
- monitoramento
- prioridades
- relatórios
- UI

#### 2.6 Introduzir help contextual de configuração
Cada etapa da configuração deve responder:
- o que falta
- por que importa
- o que fazer agora

#### 2.7 Medir tempo até first value
Registrar o funil:
- conta criada
- provider salvo
- primeiro recurso cadastrado
- primeira execução
- primeira prioridade gerada

### Critério de saída
Qualquer operador deve conseguir deixar o sistema utilizável sem navegar por áreas técnicas dispersas.

---

## Fase 3. Núcleo do Produto Principal
### Objetivo
Definir e consolidar o núcleo do SaaS de cibersegurança automatizada por IA em torno do caso de uso comercialmente mais vantajoso.

### Direção de produto
O núcleo recomendado é:
**Exposure Management / External Attack Surface Monitoring + Prioritização + Guided Remediation**

### 7 subdivisões de alteração
#### 3.1 Formalizar o inventário externo como base do produto
O sistema precisa manter:
- recursos monitorados
- mudanças de superfície
- ativos expostos
- contexto mínimo por ativo

#### 3.2 Separar detecção determinística de interpretação por IA
Tudo que for verificável por regra deve permanecer fora do LLM.

#### 3.3 Padronizar finding como unidade principal do produto
Cada finding precisa ter:
- chave estável
- categoria
- evidência
- severidade
- confiança
- sugestão
- estado

#### 3.4 Consolidar priorização do que corrigir primeiro
O produto deve responder de forma consistente:
- o que está aberto
- o que é mais crítico
- o que é mais explorável
- o que deve ser corrigido agora

#### 3.5 Padronizar guided remediation
Cada finding relevante deve produzir:
- ação resumida
- razões
- status do gate
- passo de validação

#### 3.6 Implantar verificação de correção
O sistema deve conseguir marcar:
- aberto
- resolvido
- reaberto
- regredido

#### 3.7 Formalizar prova de valor do núcleo
Métricas mínimas:
- open findings
- resolved findings
- regression count
- fix success rate
- avg time to fix

### Critério de saída
O produto já deve funcionar como control plane útil, não apenas como scanner.

---

## Fase 4. Modelo de Decisão, Contexto e Inteligência Aplicada
### Objetivo
Construir a camada inteligente que torna o produto mais valioso sem aumentar desnecessariamente o custo operacional.

### 7 subdivisões de alteração
#### 4.1 Formalizar score de risco composto
Combinar:
- criticidade técnica
- exposição
- contexto
- instabilidade
- histórico

#### 4.2 Consolidar context engine
Enriquecer priorização com:
- complexidade
- coverage quality
- instability
- asset criticality

#### 4.3 Consolidar policy engine
Definir políticas por site ou org para:
- risco máximo
- automações permitidas
- limites de ação
- regras de segurança

#### 4.4 Consolidar learning engine
Reutilizar histórico para:
- priorização futura
- recomendações recorrentes
- fechamento assistido
- redução de ruído

#### 4.5 Definir níveis de decisão
Separar:
- decisão automática
- decisão sugerida
- decisão bloqueada
- decisão que exige confirmação

#### 4.6 Melhorar confiança e explicabilidade
Toda recomendação importante precisa trazer:
- score
- confiança
- motivos
- ação recomendada

#### 4.7 Tornar o modelo de decisão economicamente sustentável
Usar IA apenas em:
- sumarização
- priorização contextual
- explicação
- relatório

### Critério de saída
As recomendações do sistema devem parecer úteis, explicáveis e comercialmente defendáveis.

---

## Fase 5. Produto, UX e Fluxos Comerciais
### Objetivo
Transformar a base técnica em um produto com alto valor percebido, onboarding claro e retenção melhor.

### 7 subdivisões de alteração
#### 5.1 Reorganizar a jornada principal
Fluxo ideal:
1. conectar
2. cadastrar
3. executar
4. revisar prioridades
5. provar melhora

#### 5.2 Expor valor ao usuário comum
Trazer para a experiência principal:
- prioridades
- progresso
- próximos passos
- readiness

#### 5.3 Criar separação correta entre operação e administração
Usuário comum vê:
- resultado
- prioridade
- execução

Admin vê:
- diagnóstico
- políticas
- logs
- laboratório

#### 5.4 Construir empty states úteis
Todo vazio deve explicar:
- o que falta
- o que vai aparecer ali
- qual ação popula a área

#### 5.5 Tornar status e erro compreensíveis
Mensagens devem explicar:
- o estado atual
- a causa provável
- o próximo passo

#### 5.6 Fortalecer a saída executiva
Construir superfícies de alto valor para:
- gestão
- renovação
- board/client communication

#### 5.7 Preparar planos e limites comerciais
Empacotar por:
- domínios
- ativos
- frequência
- usuários
- relatórios

### Critério de saída
O produto deve parecer comprável, utilizável e compreensível sem depender de explicação manual constante.

---

## Fase 6. Automação, Integrações e Eficiência Operacional
### Objetivo
Expandir a utilidade do SaaS sem destruir margem nem aumentar demais o custo de suporte.

### 7 subdivisões de alteração
#### 6.1 Padronizar automações recorrentes
Incluir:
- monitoramento recorrente
- resumo semanal
- verificação programada
- detecção de regressão

#### 6.2 Formalizar integrações de saída
Prioridade de integração:
- e-mail
- Slack
- GitHub
- Jira
- webhook

#### 6.3 Formalizar eventos e filas
Separar:
- jobs rápidos
- jobs pesados
- retries
- timeouts
- TTLs

#### 6.4 Controlar custo por tipo de execução
Distinguir:
- run interativo
- run batch
- run executivo
- run de revalidação

#### 6.5 Criar camada de observabilidade do produto
Medir:
- tempo por run
- sucesso por fluxo
- falhas por provider
- taxa de geração de prioridade
- custo estimado por org

#### 6.6 Introduzir proteção contra ruído operacional
Evitar:
- runs redundantes
- findings duplicados
- reanálises desnecessárias
- recomputação inútil

#### 6.7 Sustentar SLAs internos do produto
Definir metas mínimas para:
- tempo de resposta
- atualização de status
- taxa de falha aceitável
- reprocessamento seguro

### Critério de saída
O sistema deve operar com previsibilidade e menor desperdício, mesmo com mais clientes e automações.

---

## Fase 7. Sistemas Avançados e Escala Inteligente
### Objetivo
Construir, sobre uma base já sólida, os sistemas mais avançados e os diferenciais de longo prazo.

### 7 subdivisões de alteração
#### 7.1 Recomendação adaptativa por histórico
O sistema começa a sugerir correções com base em:
- resoluções anteriores
- contexto de cliente
- taxa de sucesso passada

#### 7.2 Playbooks assistidos
Estruturar fluxos semi-automatizados para:
- confirmação
- rechecagem
- classificação
- documentação

#### 7.3 Copiloto operacional interno
Interface que responda:
- o que mudou
- o que piorou
- o que corrigir primeiro
- como explicar isso ao cliente

#### 7.4 Segmentação por perfil de usuário
Personalizar para:
- operador
- gestor
- consultor
- MSSP
- cliente final

#### 7.5 Otimização avançada de tokens e IA
Aplicar:
- cache agressivo
- batch assíncrono
- escada de modelos
- sumarização incremental
- gating por criticidade

#### 7.6 Produto multi-tenant maduro
Aprimorar:
- isolamento lógico
- governança
- limites por plano
- white-label
- auditoria interna

#### 7.7 Sistema de expansão comercial
Construir base para:
- upsell por ativos
- upsell por frequência
- upsell por executive reporting
- upsell por automação e integrações

### Critério de saída
O produto passa de um SaaS funcional para uma plataforma de segurança automatizada com diferenciais defensáveis.

---

## Ordem Oficial de Execução
1. Fase 1. Saneamento estrutural
2. Fase 2. Base operacional e configuração
3. Fase 3. Núcleo do produto principal
4. Fase 4. Modelo de decisão e inteligência aplicada
5. Fase 5. Produto, UX e fluxos comerciais
6. Fase 6. Automação, integrações e eficiência operacional
7. Fase 7. Sistemas avançados e escala inteligente

## Regra de Governança
Cada fase deve ser executada **uma por vez**, e a fase seguinte só começa quando:

1. a anterior estiver tecnicamente estável
2. o fluxo principal correspondente estiver utilizável
3. os critérios mínimos de saída tiverem sido atendidos
4. a complexidade adicional for justificável comercialmente

## Próxima Ação Recomendada
Iniciar pela **Fase 1**, transformando este roadmap em um checklist operacional de execução com tarefas concretas por arquivo, por módulo e por impacto no sistema.

