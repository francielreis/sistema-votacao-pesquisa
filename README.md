# Sistema de Votação para Pesquisa

Aplicação Flask para simulação de pesquisa de votação. **Não é uma eleição oficial** e não possui vínculo com a Justiça Eleitoral.

## Recursos
- Cadastro de candidatos/opções pelo painel admin
- Voto simulado com limitação de um voto por dispositivo/navegador na pesquisa atual
- Apuração em tempo real com votos e percentuais
- Alteração do título da pesquisa
- Exclusão de candidatos/opções
- Zerar votos da pesquisa atual
- Banco SQLite local e PostgreSQL em produção
- Arquivo `render.yaml` pronto para deploy no Render

## Rodar localmente
```bash
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate  # Windows
pip install -r requirements.txt
python app.py
```

Acesse `http://localhost:5000`.

## Variáveis de ambiente
- `SECRET_KEY`: chave de sessão
- `ADMIN_PASSWORD`: senha do painel administrativo
- `DATABASE_URL`: conexão PostgreSQL (opcional localmente)

Em desenvolvimento, a senha padrão é `admin123`. Em produção, configure uma senha forte no Render.

## Rotas
- `/` votação
- `/resultados` apuração
- `/admin` painel administrativo
- `/admin/login` login
- `/health` verificação de saúde
