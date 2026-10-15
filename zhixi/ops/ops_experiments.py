"""Read explicitly registered isolated experiments without importing their app."""

import json
import time
import sqlite3
from pathlib import Path
from threading import Lock
from zhixi.ops.ops_snapshot import open_snapshot, read_snapshot_metadata
from zhixi.core.experiment_control import PAUSE_MARKER, read as read_control, write as write_control, process_alive

CONTROL_LOCK = Lock()
SNAPSHOT_LOCK = Lock()
SNAPSHOT_CACHE = {}


def retryable_report(directory, raw):
    """Only retry the saved local report after its previous worker has exited."""
    pid = raw.get('process_id')
    if type(pid) is not int or pid <= 0 or process_alive(pid):
        return False
    path = Path(directory) / 'history.sqlite3'
    if not path.is_file():
        return False
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute(
            'SELECT w.phase,w.selection_json,j.status FROM research_workflows w '
            'JOIN job_runs j ON j.id=w.report_job_id AND j.project_id=w.project_id '
            'JOIN research_projects p ON p.id=w.project_id WHERE w.project_id=?',
            (raw.get('project_id'),),
        ).fetchone()
    return bool(
        row
        and row[0] in {'reporting', 'report_failed'}
        and row[2] == 'failed'
        and json.loads(row[1]).get('provider') == 'local'
    )


def recoverable_report(directory, raw):
    """Read-only eligibility. A stale heartbeat alone never permits takeover."""
    pid = raw.get('process_id')
    if type(pid) is not int or pid <= 0 or process_alive(pid):
        return False
    if time.time() - raw.get('heartbeat_unix', time.time()) <= 30:
        return False
    path = Path(directory) / 'history.sqlite3'
    if not path.is_file():
        return False
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute(
            'SELECT w.phase,w.selection_json,j.status FROM research_workflows w '
            'JOIN job_runs j ON j.id=w.report_job_id AND j.project_id=w.project_id '
            'WHERE w.project_id=?',
            (raw.get('project_id'),),
        ).fetchone()
    return bool(
        row
        and row[0] in {'reporting', 'report_paused', 'report_failed'}
        and row[2] in {'queued', 'running', 'paused'}
        and json.loads(row[1]).get('provider') == 'local'
    )


def read_ops_snapshot(path):
    """Keep only display/control metadata; never cache full report payloads.

    Completed artifacts can contain hundreds of MB of reports. Stream past
    unselected payloads once per file version, without decoding them into RAM.
    """
    with SNAPSHOT_LOCK:
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
        cached = SNAPSHOT_CACHE.get(path)
        if cached and cached[0] == signature:
            return cached[1]
        with open_snapshot(path) as stream:
            metadata = read_snapshot_metadata(stream)
        after = path.stat()
        if signature != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
            raise OSError('Experiment snapshot changed during read; retry on next poll')
        SNAPSHOT_CACHE[path] = (signature, metadata)
        return metadata


def control_experiment(root, identity, action, launch=None):
    if action not in {'pause', 'resume', 'retry'}:
        raise ValueError('未知操作')
    if not identity or Path(identity).name != identity or '/' in identity or '\\' in identity:
        raise ValueError('无效实验标识')
    with CONTROL_LOCK:
        item = next((r for r in list_experiments(root) if r['id'] == identity), None)
        if item is None:
            raise ValueError('实验不存在')
        directory = Path(item['artifact_path']).parent
        if action == 'pause':
            if item['status'] not in {'running', 'pause_requested'}:
                raise ValueError('只有运行中的实验可以请求批次后暂停')
            write_control(directory, 'pause_requested')
        else:
            if action == 'retry':
                if item['status'] != 'failed' or not item.get('can_retry'):
                    raise ValueError('当前任务不能重试：请等待旧进程退出，并确认报告现场可恢复')
            elif item['status'] != 'paused' or not item.get('can_resume'):
                raise ValueError('任务尚未完成安全暂停，或不是可接续的报告实验')
            if item.get('resume_mode') == 'in_process':
                write_control(directory, 'running')
                return {'status': 'running'}
            write_control(directory, 'resuming')
            try:
                if launch:
                    launch(directory)
                else:
                    import subprocess

                    subprocess.run(
                        [
                            'powershell.exe',
                            '-NoProfile',
                            '-File',
                            str(Path(root) / 'diagnostics/start_independent_pilot.ps1'),
                            '-ExperimentDirectory',
                            str(directory),
                        ],
                        check=True,
                        capture_output=True,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                        timeout=30,
                    )
            except Exception:
                write_control(directory, 'retry_launch_failed' if action == 'retry' else 'pause_requested')
                raise
        return {'status': 'pause_requested' if action == 'pause' else 'resuming'}


def register(root, directory):
    registry = Path(root) / 'data/ops-experiments'
    registry.mkdir(parents=True, exist_ok=True)
    directory = Path(directory).resolve()
    target = registry / (directory.name + '.json')
    target.write_text(json.dumps({'directory': str(directory)}, ensure_ascii=False), encoding='utf-8')


def list_experiments(root, identity=None):
    result = []
    registry = Path(root) / 'data/ops-experiments'
    registrations = registry.glob('*.json') if identity is None else [registry / (identity + '.json')]
    for registration in registrations:
        if not registration.is_file():
            continue
        item = {'id': registration.stem, 'status': 'unavailable', 'stage': '实验状态暂不可读'}
        try:
            directory = Path(json.loads(registration.read_text(encoding='utf-8'))['directory'])
            path = directory / 'result.json'
            raw = read_ops_snapshot(path)
            snapshot = (raw.get('snapshots') or [{}])[-1]
            workflow = snapshot.get('workflow') or {}
            phase = workflow.get('phase', '')
            terminal = raw.get('terminal_observation') or {}
            # Probe writers publish their lifecycle separately from their work
            # phase. A completed "evaluating" probe no longer owes a heartbeat.
            # Research completion is different: collecting may end before reporting.
            evaluation_status = snapshot.get('status') if phase == 'evaluating' else None
            if evaluation_status == 'completed':
                phase = 'evaluation_ready'
            status = (
                'failed'
                if raw.get('error') or phase == 'report_failed'
                else evaluation_status
                if evaluation_status in {'failed', 'cancelled', 'paused'}
                else 'completed'
                if phase in {'report_ready', 'collection_ready', 'evaluation_ready'}
                else 'paused'
                if phase in {'paused', 'report_paused'}
                else 'cancelled'
                if snapshot.get('status') == 'cancelled'
                else 'running'
            )
            item.update(
                goal=raw.get('goal', directory.name),
                project_id=raw.get('project_id'),
                status=status,
                phase=phase,
                stage=workflow.get('stage') or snapshot.get('intent_stage') or phase,
                elapsed_seconds=raw.get('seconds', snapshot.get('seconds', 0)),
                updated_at=path.stat().st_mtime,
                pages=snapshot.get('page_states', {}),
                error=raw.get('error')
                or workflow.get('error')
                or (raw.get('error_type', '') if status == 'failed' else ''),
                artifact_path=str(path),
                terminal=bool(terminal) or status in {'failed', 'completed', 'cancelled'},
            )
            if evaluation_status is not None:
                item.update(completed_calls=raw.get('completed'), total_calls=raw.get('total'))
            heartbeat = raw.get('heartbeat_unix')
            if status == 'running' and isinstance(heartbeat, (int, float)):
                age = max(0, time.time() - heartbeat)
                item.update(heartbeat_age_seconds=round(age, 1), process_id=raw.get('process_id'))
                if age > 30:
                    item.update(
                        status='interrupted',
                        stage='实验心跳已中断，当前不能确认仍在运行',
                        error='运行状态未更新；保留现场，不自动重启。',
                        terminal=False,
                    )
            elif status == 'running' and time.time() - path.stat().st_mtime > 120:
                item.update(status='unavailable', stage='旧实验缺少心跳，不能仅凭旧快照判断仍在运行')
            control = read_control(directory)
            marker = PAUSE_MARKER in str(raw.get('error', '')) + str(workflow.get('error', ''))
            if (
                not marker
                and phase == 'report_failed'
                and control.get('state') in {'pause_requested', 'resuming'}
            ):
                # Older workers classify the marker into a generic UI message;
                # use only the exact linked job's retained technical detail.
                try:
                    with sqlite3.connect(
                        (directory / 'history.sqlite3').as_uri() + '?mode=ro', uri=True
                    ) as db:
                        row = db.execute(
                            'SELECT error_json FROM job_runs WHERE id=?', (workflow.get('report_job_id'),)
                        ).fetchone()
                    marker = bool(row and PAUSE_MARKER in (row[0] or ''))
                except sqlite3.Error:
                    pass  # Unknown or unreadable errors are never called a safe pause.
            if control.get('state') == 'parked' and item['status'] == 'running':
                item.update(
                    status='paused',
                    stage='当前批次已保存；已暂停后续模型调用',
                    can_resume=True,
                    resume_mode='in_process',
                    terminal=False,
                )
            elif control.get('state') == 'resuming' and control['updated'] > raw.get(
                'process_started_at_unix', 0
            ):
                item.update(status='resuming', stage='正在从已保存批次继续', error='')
                if time.time() - control['updated'] > 60:
                    if status == 'failed':
                        item.update(
                            status='failed',
                            stage='重试尚未启动成功，现场仍保留',
                            error=raw.get('error') or workflow.get('error', ''),
                        )
                    else:
                        item.update(
                            status='paused', stage='接续未启动成功，现场仍保留，可再次继续', can_resume=True
                        )
            elif marker and terminal:
                exited = not process_alive(raw.get('process_id'))
                item.update(
                    status='paused' if exited else 'pause_requested',
                    stage='已完成上一批并保存，等待继续' if exited else '批次已保存，正在释放旧工作进程',
                    error='',
                    can_resume=exited and phase == 'report_failed',
                    terminal=False,
                )
            elif control.get('state') == 'pause_requested' and item['status'] == 'running':
                item.update(status='pause_requested', stage='完成当前批次后暂停；正在保存前不会中断模型')
            if item['status'] in {'interrupted', 'unavailable', 'paused'} and not item.get('can_resume'):
                if recoverable_report(directory, raw):
                    item.update(
                        status='paused',
                        stage='实验进程已退出；可从已保存批次继续报告',
                        error='',
                        can_resume=True,
                        resume_mode='restart_report',
                        terminal=False,
                    )
            if item['status'] == 'failed':
                item['can_retry'] = retryable_report(directory, raw)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
            pass  # A live writer may be between truncate and write; do not call that a model failure.
        except ImportError:
            item.update(stage='OPS 状态读取依赖缺失，请按 README 安装 requirements-ops.txt')
        result.append(item)
    return sorted(result, key=lambda row: row.get('updated_at', 0), reverse=True)
