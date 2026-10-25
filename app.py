import os
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import wraps
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-change-me')

database_url = os.getenv('DATABASE_URL', '').strip().strip('"').strip("'")
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
if database_url.startswith('postgresql+psycopg://'):
    database_url = database_url.replace('postgresql+psycopg://', 'postgresql+psycopg2://', 1)

valid_db_prefixes = ('postgresql://', 'postgresql+psycopg2://', 'sqlite:///', 'sqlite://')
if not database_url or not database_url.startswith(valid_db_prefixes):
    database_url = 'sqlite:///votacao.db'

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

VALID_NUMBERS = {'13', '22'}
SPECIAL_CANDIDATES = {
    'branco': {'number': '__BRANCO__', 'name': 'Voto em Branco'},
    'nulo': {'number': '__NULO__', 'name': 'Voto Nulo'},
}


# Apuração oficial do 2º turno presidencial de 2026.
# A URL pode ser sobrescrita no Render pela variável TSE_RESULT_URL sem alterar o código.
TSE_RESULT_URL = os.getenv(
    'TSE_RESULT_URL',
    'https://resultados.tse.jus.br/oficial/ele2026/6258/dados/br/br-c0001-e006258-u.json'
)
TSE_CACHE_SECONDS = max(5, int(os.getenv('TSE_CACHE_SECONDS', '10')))
TSE_TIMEOUT_SECONDS = max(3, int(os.getenv('TSE_TIMEOUT_SECONDS', '8')))

# Fotos oficiais publicadas pelo TSE para os candidatos do 2º turno.
# Servem como fallback quando nenhuma foto personalizada foi cadastrada no admin.
OFFICIAL_CANDIDATE_PHOTOS = {
    '13': 'https://www.tse.jus.br/imagens/imagens/lula/%40%40images/image-200-93d51d895f8ebc8900bf3e02c7052bef.png',
    '22': 'https://www.tse.jus.br/imagens/imagens/flavio-bolsonaro/%40%40images/image-200-7092d276831e3453f70c59d07d5c9ae7.png',
}

_tse_cache = {
    'data': None,
    'fetched_at': 0.0,
    'etag': None,
    'last_modified': None,
}
_tse_lock = threading.Lock()


def _safe_int(value):
    try:
        return int(str(value or '0').replace('.', '').strip())
    except (TypeError, ValueError):
        return 0


def _safe_pct(value):
    try:
        return float(str(value or '0').replace(',', '.').strip())
    except (TypeError, ValueError):
        return 0.0


def _normalize_tse_results(payload):
    candidates = []

    for office in payload.get('carg', []):
        if str(office.get('cd')) != '1':
            continue

        for alliance in office.get('agr', []):
            for party in alliance.get('par', []):
                for candidate in party.get('cand', []):
                    candidates.append({
                        'number': str(candidate.get('n') or ''),
                        'name': candidate.get('nmu') or candidate.get('nm') or 'Candidato',
                        'full_name': candidate.get('nm') or '',
                        'party': party.get('sg') or '',
                        'votes': _safe_int(candidate.get('vap')),
                        'pct': _safe_pct(candidate.get('pvap')),
                        'status': candidate.get('st') or '',
                        'elected': candidate.get('e') == 's',
                    })

    candidates.sort(key=lambda item: (-item['votes'], item['name']))

    sections = payload.get('s') or {}
    voters = payload.get('e') or {}
    votes = payload.get('v') or {}
    valid_votes = sum(candidate['votes'] for candidate in candidates)
    blank_votes = _safe_int(votes.get('vb'))
    null_votes = _safe_int(votes.get('tvn') or votes.get('vn'))
    total_votes = _safe_int(votes.get('tv')) or (valid_votes + blank_votes + null_votes)

    return {
        'ok': True,
        'source': 'Tribunal Superior Eleitoral (TSE)',
        'source_url': TSE_RESULT_URL,
        'election': str(payload.get('ele') or '6258'),
        'round': str(payload.get('t') or '2'),
        'generated_date': payload.get('dg') or payload.get('dt') or '',
        'generated_time': payload.get('hg') or payload.get('ht') or '',
        'sections': {
            'total': _safe_int(sections.get('ts')),
            'counted': _safe_int(sections.get('st')),
            'pct': _safe_pct(sections.get('pst')),
        },
        'electorate': {
            'total': _safe_int(voters.get('te')),
            'counted': _safe_int(voters.get('est')),
        },
        'votes': {
            'total': total_votes,
            'valid': valid_votes,
            'blank': blank_votes,
            'null': null_votes,
        },
        'candidates': candidates,
        'stale': False,
    }


def fetch_tse_results():
    now = time.monotonic()

    if _tse_cache['data'] and now - _tse_cache['fetched_at'] < TSE_CACHE_SECONDS:
        return _tse_cache['data']

    with _tse_lock:
        now = time.monotonic()
        if _tse_cache['data'] and now - _tse_cache['fetched_at'] < TSE_CACHE_SECONDS:
            return _tse_cache['data']

        headers = {
            'Accept': 'application/json',
            'User-Agent': 'ReisTech-Apuracao/1.0 (+https://resultados.tse.jus.br)'
        }
        if _tse_cache['etag']:
            headers['If-None-Match'] = _tse_cache['etag']
        if _tse_cache['last_modified']:
            headers['If-Modified-Since'] = _tse_cache['last_modified']

        request_tse = Request(TSE_RESULT_URL, headers=headers)

        try:
            with urlopen(request_tse, timeout=TSE_TIMEOUT_SECONDS) as response:
                raw = response.read().decode('utf-8')
                payload = json.loads(raw)
                normalized = _normalize_tse_results(payload)
                _tse_cache['data'] = normalized
                _tse_cache['fetched_at'] = now
                _tse_cache['etag'] = response.headers.get('ETag')
                _tse_cache['last_modified'] = response.headers.get('Last-Modified')
                return normalized
        except HTTPError as exc:
            if exc.code == 304 and _tse_cache['data']:
                _tse_cache['fetched_at'] = now
                return _tse_cache['data']
            if _tse_cache['data']:
                stale = dict(_tse_cache['data'])
                stale['stale'] = True
                stale['warning'] = 'O TSE não respondeu agora; exibindo a última atualização recebida.'
                return stale
            raise
        except (URLError, TimeoutError, json.JSONDecodeError):
            if _tse_cache['data']:
                stale = dict(_tse_cache['data'])
                stale['stale'] = True
                stale['warning'] = 'O TSE não respondeu agora; exibindo a última atualização recebida.'
                return stale
            raise


# Agregação por Grandes Regiões do Brasil (IBGE).
REGION_STATES = {
    'Norte': ['ac', 'ap', 'am', 'pa', 'ro', 'rr', 'to'],
    'Nordeste': ['al', 'ba', 'ce', 'ma', 'pb', 'pe', 'pi', 'rn', 'se'],
    'Centro-Oeste': ['df', 'go', 'mt', 'ms'],
    'Sudeste': ['es', 'mg', 'rj', 'sp'],
    'Sul': ['pr', 'rs', 'sc'],
}
TSE_REGION_CACHE_SECONDS = max(20, int(os.getenv('TSE_REGION_CACHE_SECONDS', '30')))
_region_cache = {'data': None, 'fetched_at': 0.0}
_region_lock = threading.Lock()


def _state_result_url(uf):
    return (
        'https://resultados.tse.jus.br/oficial/ele2026/6258/dados/'
        f'{uf}/{uf}-c0001-e006258-u.json'
    )


def _fetch_json_url(url):
    req = Request(url, headers={
        'Accept': 'application/json',
        'User-Agent': 'ReisTech-Apuracao/1.0 (+https://resultados.tse.jus.br)'
    })
    with urlopen(req, timeout=TSE_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode('utf-8'))


def _fetch_state_official_results(uf):
    payload = _fetch_json_url(_state_result_url(uf))
    return uf, _normalize_tse_results(payload)


def fetch_region_results():
    now = time.monotonic()
    if _region_cache['data'] and now - _region_cache['fetched_at'] < TSE_REGION_CACHE_SECONDS:
        return _region_cache['data']

    with _region_lock:
        now = time.monotonic()
        if _region_cache['data'] and now - _region_cache['fetched_at'] < TSE_REGION_CACHE_SECONDS:
            return _region_cache['data']

        uf_to_region = {
            uf: region
            for region, states in REGION_STATES.items()
            for uf in states
        }
        state_results = {}
        errors = []

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(_fetch_state_official_results, uf): uf
                for uf in uf_to_region
            }
            for future in as_completed(futures):
                uf = futures[future]
                try:
                    state_results[uf] = future.result()[1]
                except Exception:
                    errors.append(uf)

        if errors:
            if _region_cache['data']:
                stale = dict(_region_cache['data'])
                stale['stale'] = True
                stale['warning'] = (
                    'Alguns arquivos estaduais do TSE não responderam; '
                    'exibindo a última consolidação regional completa.'
                )
                return stale
            raise RuntimeError('Falha ao consultar UFs: ' + ', '.join(sorted(errors)))

        regions = []
        for region_name, states in REGION_STATES.items():
            candidate_totals = {}
            total_sections = 0
            counted_sections = 0
            electorate = 0
            blank_votes = 0
            null_votes = 0

            for uf in states:
                state = state_results[uf]
                total_sections += state['sections']['total']
                counted_sections += state['sections']['counted']
                electorate += state['electorate']['total']
                blank_votes += state['votes']['blank']
                null_votes += state['votes']['null']

                for candidate in state['candidates']:
                    number = candidate['number']
                    if number not in candidate_totals:
                        candidate_totals[number] = {
                            'number': number,
                            'name': candidate['name'],
                            'party': candidate['party'],
                            'votes': 0,
                        }
                    candidate_totals[number]['votes'] += candidate['votes']

            candidates = list(candidate_totals.values())
            valid_votes = sum(candidate['votes'] for candidate in candidates)
            for candidate in candidates:
                candidate['pct'] = (
                    candidate['votes'] / valid_votes * 100
                    if valid_votes else 0.0
                )
            candidates.sort(key=lambda item: (-item['votes'], item['number']))

            regions.append({
                'name': region_name,
                'states': [uf.upper() for uf in states],
                'sections': {
                    'total': total_sections,
                    'counted': counted_sections,
                    'pct': (
                        counted_sections / total_sections * 100
                        if total_sections else 0.0
                    ),
                },
                'electorate': electorate,
                'votes': {
                    'valid': valid_votes,
                    'blank': blank_votes,
                    'null': null_votes,
                    'total': valid_votes + blank_votes + null_votes,
                },
                'candidates': candidates,
            })

        data = {
            'ok': True,
            'source': 'Tribunal Superior Eleitoral (TSE)',
            'regions': regions,
            'stale': False,
            'refresh_seconds': TSE_REGION_CACHE_SECONDS,
        }
        _region_cache['data'] = data
        _region_cache['fetched_at'] = now
        return data


class Poll(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(180), nullable=False, default='Pesquisa de intenção de voto')
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Candidate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    number = db.Column(db.String(10), nullable=True)
    photo_url = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    votes = db.relationship('Vote', backref='candidate', cascade='all, delete-orphan', lazy=True)


class Vote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    poll_id = db.Column(db.Integer, db.ForeignKey('poll.id'), nullable=False)
    candidate_id = db.Column(db.Integer, db.ForeignKey('candidate.id'), nullable=False)
    voter_hash = db.Column(db.String(64), nullable=False)
    device_hash = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('poll_id', 'voter_hash', name='uq_poll_voter'),
    )


def current_poll():
    poll = Poll.query.filter_by(active=True).order_by(Poll.id.desc()).first()
    if not poll:
        poll = Poll(title='Pesquisa de intenção de voto', active=True)
        db.session.add(poll)
        db.session.commit()
    return poll


def voter_fingerprint():
    # Identificador anônimo por navegador/sessão.
    token = session.get('voter_token')
    if not token:
        token = os.urandom(32).hex()
        session['voter_token'] = token
    raw = f"{token}|{app.config['SECRET_KEY']}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def device_fingerprint():
    # Camada adicional para dificultar votos repetidos após limpar cookies.
    # Não armazena IP, user-agent ou idioma em texto puro: apenas um hash.
    forwarded = request.headers.get('X-Forwarded-For', '')
    ip = forwarded.split(',')[0].strip() if forwarded else (request.remote_addr or '')
    ua = request.headers.get('User-Agent', '')
    language = request.headers.get('Accept-Language', '')
    platform = request.headers.get('Sec-CH-UA-Platform', '')
    mobile = request.headers.get('Sec-CH-UA-Mobile', '')
    raw = f"{ip}|{ua}|{language}|{platform}|{mobile}|{app.config['SECRET_KEY']}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('admin_ok'):
            return redirect(url_for('admin_login'))
        return fn(*args, **kwargs)
    return wrapper


def get_special_candidate(kind):
    data = SPECIAL_CANDIDATES[kind]
    candidate = Candidate.query.filter_by(number=data['number']).first()
    if not candidate:
        candidate = Candidate(name=data['name'], number=data['number'])
        db.session.add(candidate)
        db.session.commit()
    return candidate


def register_vote(candidate, success_message='Voto simulado registrado com sucesso.'):
    poll = current_poll()
    admin_test_mode = bool(session.get('admin_ok'))

    if admin_test_mode:
        # No modo de teste do administrador, cada confirmação recebe um identificador
        # único para permitir vários votos sem liberar essa possibilidade ao público.
        raw = f"admin-test|{os.urandom(32).hex()}|{app.config['SECRET_KEY']}"
        fp = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    else:
        fp = voter_fingerprint()
        device_fp = device_fingerprint()
        repeated = Vote.query.filter_by(poll_id=poll.id, voter_hash=fp).first()
        if not repeated:
            repeated = Vote.query.filter_by(poll_id=poll.id, device_hash=device_fp).first()

        if repeated:
            flash('Só é permitida uma simulação por pessoa/dispositivo nesta pesquisa. Reis Tech Software • Desenvolvedor: Franciel Reis', 'duplicate-warning')
            return redirect(url_for('results'))

    db.session.add(Vote(
        poll_id=poll.id,
        candidate_id=candidate.id,
        voter_hash=fp,
        device_hash=None if admin_test_mode else device_fp,
    ))
    db.session.commit()

    if admin_test_mode:
        flash(f'{success_message} Modo teste do administrador: novo voto permitido.', 'success')
    else:
        flash(success_message, 'success')
    return redirect(url_for('results'))


@app.route('/')
def index():
    poll = current_poll()
    candidates = Candidate.query.filter(Candidate.number.in_(VALID_NUMBERS)).order_by(
        Candidate.number.asc(), Candidate.name.asc()
    ).all()
    admin_test_mode = bool(session.get('admin_ok'))
    already_voted = False if admin_test_mode else (
        Vote.query.filter_by(poll_id=poll.id, voter_hash=voter_fingerprint()).first() is not None
    )
    return render_template(
        'index.html',
        poll=poll,
        candidates=candidates,
        already_voted=already_voted,
        admin_test_mode=admin_test_mode,
    )


@app.post('/votar/<int:candidate_id>')
def vote(candidate_id):
    candidate = Candidate.query.get_or_404(candidate_id)
    if candidate.number not in VALID_NUMBERS:
        flash('Número inválido. Use 13, 22, BRANCO ou confirme um número diferente como NULO.', 'warning')
        return redirect(url_for('index'))
    return register_vote(candidate)


@app.post('/votar-especial/<kind>')
def vote_special(kind):
    if kind not in SPECIAL_CANDIDATES:
        return redirect(url_for('index'))

    candidate = get_special_candidate(kind)
    if kind == 'branco':
        return register_vote(candidate, 'Voto em branco registrado com sucesso.')
    return register_vote(candidate, 'Voto nulo registrado com sucesso.')


@app.route('/resultados')
def results():
    poll = current_poll()
    total = Vote.query.filter_by(poll_id=poll.id).count()
    candidates = Candidate.query.filter(Candidate.number.in_(VALID_NUMBERS)).order_by(
        Candidate.number.asc(), Candidate.name.asc()
    ).all()

    rows = []
    for c in candidates:
        count = Vote.query.filter_by(poll_id=poll.id, candidate_id=c.id).count()
        pct = (count / total * 100) if total else 0
        rows.append({'candidate': c, 'votes': count, 'pct': pct})
    rows.sort(key=lambda x: x['votes'], reverse=True)

    blank_candidate = Candidate.query.filter_by(number=SPECIAL_CANDIDATES['branco']['number']).first()
    null_candidate = Candidate.query.filter_by(number=SPECIAL_CANDIDATES['nulo']['number']).first()
    blank_votes = Vote.query.filter_by(poll_id=poll.id, candidate_id=blank_candidate.id).count() if blank_candidate else 0
    null_votes = Vote.query.filter_by(poll_id=poll.id, candidate_id=null_candidate.id).count() if null_candidate else 0
    blank_pct = (blank_votes / total * 100) if total else 0
    null_pct = (null_votes / total * 100) if total else 0

    return render_template(
        'results.html',
        poll=poll,
        rows=rows,
        total=total,
        blank_votes=blank_votes,
        null_votes=null_votes,
        blank_pct=blank_pct,
        null_pct=null_pct,
    )


def _attach_candidate_photos(data):
    """Anexa fotos cadastradas no simulador aos candidatos oficiais pelo número."""
    try:
        photo_map = {
            str(candidate.number): candidate.photo_url
            for candidate in Candidate.query.filter(Candidate.photo_url.isnot(None)).all()
            if candidate.number and candidate.photo_url
        }
    except Exception:
        photo_map = {}

    def apply(candidates):
        for candidate in candidates or []:
            number = str(candidate.get('number') or '')
            custom_photo = photo_map.get(number)
            official_photo = OFFICIAL_CANDIDATE_PHOTOS.get(number, '')
            candidate['photo_url'] = custom_photo or official_photo
            candidate['photo_source'] = 'cadastro' if custom_photo else ('TSE' if official_photo else '')

    apply(data.get('candidates'))
    for region in data.get('regions', []) or []:
        apply(region.get('candidates'))
    return data


@app.get('/apuracao-real')
def official_results():
    official_data = None
    regions_data = None
    try:
        official_data = _attach_candidate_photos(fetch_tse_results())
    except Exception:
        official_data = None
    try:
        regions_data = _attach_candidate_photos(fetch_region_results())
    except Exception:
        regions_data = None

    return render_template(
        'official_results.html',
        title='Apuração Oficial em Tempo Real',
        official_page=True,
        official_data=official_data,
        regions_data=regions_data,
    )


@app.get('/api/apuracao-real')
def official_results_api():
    try:
        data = _attach_candidate_photos(fetch_tse_results())
        return jsonify(data)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return jsonify({
            'ok': False,
            'error': 'Não foi possível consultar o TSE neste momento. Tente novamente em instantes.'
        }), 503



@app.get('/api/apuracao-regioes')
def official_regions_api():
    try:
        return jsonify(_attach_candidate_photos(fetch_region_results()))
    except Exception:
        return jsonify({
            'ok': False,
            'error': 'Não foi possível consolidar os resultados regionais do TSE neste momento.'
        }), 503


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password', '')
        expected = os.getenv('ADMIN_PASSWORD', 'admin123')
        if password == expected:
            session['admin_ok'] = True
            return redirect(url_for('admin'))
        flash('Senha inválida.', 'danger')
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/admin', methods=['GET', 'POST'])
@admin_required
def admin():
    poll = current_poll()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'poll_title':
            title = request.form.get('title', '').strip()
            if title:
                poll.title = title[:180]
                db.session.commit()
                flash('Título atualizado.', 'success')
        elif action == 'add_candidate':
            name = request.form.get('name', '').strip()
            number = request.form.get('number', '').strip()
            photo_url = request.form.get('photo_url', '').strip()
            if not name:
                flash('Informe o nome do candidato/opção.', 'danger')
            else:
                db.session.add(Candidate(name=name[:120], number=number[:10] or None, photo_url=photo_url[:500] or None))
                db.session.commit()
                flash('Candidato/opção adicionado.', 'success')
        elif action == 'reset_votes':
            Vote.query.filter_by(poll_id=poll.id).delete()
            db.session.commit()
            flash('Votos da pesquisa atual foram zerados.', 'success')

    candidates = Candidate.query.filter(~Candidate.number.in_([SPECIAL_CANDIDATES['branco']['number'], SPECIAL_CANDIDATES['nulo']['number']])).order_by(Candidate.number.asc(), Candidate.name.asc()).all()
    total = Vote.query.filter_by(poll_id=poll.id).count()
    return render_template('admin.html', poll=poll, candidates=candidates, total=total)


@app.post('/admin/candidato/<int:candidate_id>/excluir')
@admin_required
def delete_candidate(candidate_id):
    candidate = Candidate.query.get_or_404(candidate_id)
    db.session.delete(candidate)
    db.session.commit()
    flash('Candidato/opção removido.', 'success')
    return redirect(url_for('admin'))


@app.get('/health')
def health():
    return {
        'status': 'ok',
        'database': db.engine.url.get_backend_name()
    }


with app.app_context():
    db.create_all()

    # Migração leve para instalações já existentes, sem apagar votos.
    columns = {col['name'] for col in inspect(db.engine).get_columns('vote')}
    if 'device_hash' not in columns:
        db.session.execute(text('ALTER TABLE vote ADD COLUMN device_hash VARCHAR(64)'))
        db.session.commit()

    db.session.execute(text(
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_poll_device_hash '
        'ON vote (poll_id, device_hash) WHERE device_hash IS NOT NULL'
    ))
    db.session.commit()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=os.getenv('FLASK_DEBUG') == '1')
