# LocalAI e o que as pessoas reclamam (e o que fizemos)

## O LocalAI é concorrente?

**Só em parte.** O [LocalAI](https://localai.io/) é um servidor de código aberto,
auto-hospedado, com API compatível com a da OpenAI: o desenvolvedor roda modelos
(texto, voz, imagem, embeddings) no próprio hardware e aponta seus aplicativos
para ele. É **infraestrutura para quem programa**. O Jarvis é um **assistente
pronto para quem não programa**, que conversa, lembra e cuida dos computadores
da pessoa.

| | LocalAI | Jarvis |
|---|---|---|
| Público | Desenvolvedores e entusiastas | Pessoas comuns e pequenos negócios |
| Instalação | Docker/binário, escolher a imagem certa para a GPU | Um comando colado no terminal |
| Onde a IA roda | No seu hardware (precisa de máquina boa) | Na nuvem (funciona em qualquer PC) |
| Privacidade | Dados ficam em casa | Pedidos passam pela IA da Anthropic |
| Faz coisas nos seus PCs | Não (é só uma API) | Sim, com aprovação e modo seguro |
| Custo | Grátis + hardware + tempo | Assinatura por créditos |

O espaço de sobreposição é "IA que ajuda no meu computador". A vantagem do
LocalAI é privacidade total e custo zero de uso; a nossa é não exigir nenhum
conhecimento técnico nem hardware potente. Quem quer privacidade absoluta não é
o nosso cliente, e por isso a página fala abertamente de onde os dados passam.

## O que as pessoas reclamam

Só encontramos reclamações em issues públicas do GitHub e em textos de
comparação; as buscas **não** trouxeram threads de Reddit nem dados
quantitativos. Trate o que segue como padrões recorrentes, não como pesquisa.

1. **GPU e Docker que não funcionam.** Contêiner que não enxerga a placa de
   vídeo ([#5063](https://github.com/mudler/LocalAI/issues/5063)), falha ao
   carregar modelo na imagem CUDA 13
   ([#10625](https://github.com/mudler/LocalAI/issues/10625)), driver
   incompatível em placa nova
   ([#5189](https://github.com/mudler/LocalAI/issues/5189)). Os próprios
   mantenedores reconhecem que muita gente enfrenta problemas para instalar e
   propuseram um script que escolhe a imagem certa.
2. **Documentação que não bate com a instalação.** Em
   [#5225](https://github.com/mudler/LocalAI/issues/5225), uma instalação limpa
   não roda nenhum modelo: os nomes de variáveis da documentação diferem do
   arquivo instalado, e o instalador cria um serviço que a documentação não cita.
3. **Erros que não dizem o que fazer.** "could not load model - all backends
   returned error" ([#1037](https://github.com/go-skynet/LocalAI/issues/1037)),
   downloads da galeria que nunca terminam, arquivo de modelo que some
   ([#9683](https://github.com/mudler/LocalAI/issues/9683)) e uma enxurrada de
   erros genéricos para um modelo que simplesmente não está instalado
   ([#1076](https://github.com/go-skynet/LocalAI/issues/1076)).
4. **Mais técnico que as alternativas.** Comparações descrevem o LM Studio como
   a opção mais fácil para iniciantes, o Ollama como intermediária e o LocalAI
   como a mais técnica.
5. **Problemas gerais de IA local para leigos** (textos de comparação e fóruns):
   exigência de hardware caro, ajustes de configuração, e comentários de que a
   conveniência costuma vencer a privacidade e de que falta controle de
   permissões que um leigo entenda.

## O que aplicamos no Jarvis

| Reclamação | O que fizemos |
|---|---|
| Docker/GPU/drivers | Não existe: a IA roda na nuvem; o agente só precisa de Python, que o instalador cuida no Windows. Dito na página inicial. |
| Instalação com passos demais | Um comando por sistema (Windows, Mac, Linux), com o código embutido, testado de ponta a ponta no GitHub Actions nos três sistemas. |
| Documentação desencontrada | Não há arquivo de configuração para o usuário editar; a tela "Conectar máquina" é o único passo e explica como abrir o terminal. |
| Erros genéricos | Toda tarefa que falha mostra "O que aconteceu" em português com o próximo passo (`nuvem/falhas.py`: sem permissão, disco cheio, programa inexistente, sem internet, máquina desligou, limite da conta de IA...). O Jarvis recebe a mesma explicação ao acompanhar a tarefa. |
| Não sei por que não conectou | Depois de 90 s sem a máquina aparecer, o painel mostra um passo a passo e o comando `agente.py --diagnostico`, que confere Python, configuração, internet, resposta do Jarvis e a chave, e diz em português o que corrigir. |
| Falta de controle de permissões compreensível | Modo seguro por máquina, ações prontas descritas em linguagem simples, aprovação antes de rodar e modo livre só com a senha. |
| Privacidade vs. conveniência | Aviso transparente na página: os pedidos passam pela IA da Anthropic. Exclusão de conta apaga tudo. |

## Ainda não resolvemos (próximos candidatos)

- **Privacidade total:** quem quer tudo local não é atendido. Uma opção futura é
  permitir que o agente use um modelo local (por exemplo Ollama) para tarefas
  simples, mantendo a nuvem para as difíceis.
- **Funcionar sem internet:** hoje o Jarvis depende dela.
- **Custo previsível:** o modelo de créditos precisa de um painel de consumo
  mais claro por tarefa.

## Fontes
- [localai.io](https://localai.io/)
- [mudler/LocalAI no GitHub](https://github.com/mudler/LocalAI/issues)
- Comparações: [Reintech](https://reintech.io/blog/how-to-run-llms-locally-ollama-vs-lm-studio-vs-localai-comparison), [BytePlus](https://www.byteplus.com/en/topic/398253)
