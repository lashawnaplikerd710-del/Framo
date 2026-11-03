"""Thin CLI for the independent TaskService; no editing business logic here."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path

from .runtime import doctor, load_config, run as legacy_tool_loop
from .provider import ProviderError


ROOT = Path(__file__).resolve().parents[1]


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), flush=True)


def _native_call(args, config, operation, *, with_host=False):
    """A launch happens only when the caller explicitly supplies the opt-in flag."""
    if not args.isolated_host:
        if with_host:raise ValueError('connection diagnose requires --isolated-host and --host-out')
        if args.host_out:raise ValueError('--host-out requires --isolated-host')
        return operation()
    if not args.host_out:raise ValueError('--isolated-host requires a new --host-out directory')
    host_out=Path(args.host_out).resolve()
    if getattr(args,'draft',None) and host_out.is_relative_to(Path(args.draft).resolve()):
        raise ValueError('Host report directory must be outside the input draft')
    from .native_host import IsolatedNativeHost
    with IsolatedNativeHost(config['install_dir'],host_out,allow_isolated_host=True,
                            user_data=config.get('user_data_dir')) as host:
        result=operation(host) if with_host else operation()
        host.ensure_running()
    result['managed_host']={'report_path':str(host_out/'host-lifecycle.json'),
                            'status':host.report['status'],
                            'scope':'Host lifecycle only; not resource entitlement or delivery acceptance'}
    return result


def main():
    with ExitStack() as resources:
        return _main(resources)


def _main(resources):
    parser = argparse.ArgumentParser(prog='agent', description='独立视频自动剪辑 Agent')
    parser.add_argument('--config', default=str(ROOT / 'config.native.json'))
    parser.add_argument('--state-dir', default=str(ROOT / 'data'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    voice_cmd=sub.add_parser('new-narration',help='DeepSeek writes fresh copy; Jianying synthesizes editable native voice/captions')
    voice_cmd.add_argument('--draft',required=True);voice_cmd.add_argument('--out',required=True)
    voice_cmd.add_argument('--instruction',default='按实际镜头创作新的中文带货口播，不复用原素材口播或照读原字幕。')
    voice_cmd.add_argument('--name',default='全新口播修订')
    voice_cmd.add_argument('--narration-track');voice_cmd.add_argument('--caption-track')
    voice_cmd.add_argument('--resume',action='store_true');voice_cmd.add_argument('--register',action='store_true')
    connection = sub.add_parser('connection', help='Check the official native host; optional explicit isolated-host startup')
    connection.add_argument('action', choices=['check', 'diagnose'])
    export_cmd = sub.add_parser('export', help='Try the official native export; optional isolated host, no replacement encoder')
    export_cmd.add_argument('--draft', required=True)
    export_cmd.add_argument('--out', required=True)
    resources_cmd = sub.add_parser('resources', help='Internal resource readiness, or read-only official search')
    resource_action = resources_cmd.add_mutually_exclusive_group()
    resource_action.add_argument('--search')
    resource_action.add_argument('--acquire', help='Query and download one explicitly nonmember official resource ID')
    resources_cmd.add_argument('--out', help='New short ASCII directory for downloaded resource and provenance')
    resources_cmd.add_argument('--type', default='sticker',
                               choices=['music','sound','sticker','effect','filter','transition','text_template'])
    package_cmd = sub.add_parser('native-package', help='Apply an explicit official-resource plan to a new draft with checkpoint recovery')
    package_cmd.add_argument('--draft', required=True)
    package_cmd.add_argument('--plan', required=True)
    package_cmd.add_argument('--out', required=True)
    package_cmd.add_argument('--resume', action='store_true')
    package_cmd.add_argument('--isolated-host', action='store_true', required=True,
                             help='Explicit opt-in for the real isolated host managed by this workflow')
    for host_cmd in (connection, export_cmd, resources_cmd):
        host_cmd.add_argument('--isolated-host', action='store_true',
                              help='Explicit opt-in: launch the real app on an inactive desktop for this command')
        host_cmd.add_argument('--host-out', help='New directory for owned-host lifecycle and index backup')
    model = sub.add_parser('model')
    model.add_argument('action', choices=['check'])
    model.add_argument('--stage', choices=['all', 'image'], default='all',
                       help='Run all connection checks or only the image probe')
    edit = sub.add_parser('edit')
    edit.add_argument('--input', action='append', required=True,
                      help='Repeat for each source when using a multi-source montage workflow')
    edit.add_argument('--instruction', required=True)
    edit.add_argument('--workflow', choices=['talking_head', 'interview', 'product_montage', 'product_ad', 'custom', 'crossborder_montage'], default='talking_head')
    edit.add_argument('--target-country', help='Required for crossborder_montage; localized copy is generated for this market')
    edit.add_argument('--skip-publish', action='store_true',
                      help='Keep the checked draft in the task directory; publish it later')
    for action in ('status', 'cancel', 'resume', 'rerun', 'inspect', 'publish', 'open'):
        command = sub.add_parser(action)
        command.add_argument('--job', required=True)
    batch = sub.add_parser('batch', help='Create or inspect up to fifteen independent outputs')
    batch_action = batch.add_subparsers(dest='batch_action', required=True)
    batch_create = batch_action.add_parser('create')
    batch_create.add_argument('--spec', required=True, help='JSON file containing an outputs list')
    from .batch_model import DEFAULT_GAP_US
    batch_create.add_argument('--gap-us', type=int, default=DEFAULT_GAP_US)
    batch_status = batch_action.add_parser('status'); batch_status.add_argument('--batch', required=True)
    batch_retry = batch_action.add_parser('retry'); batch_retry.add_argument('--batch', required=True); batch_retry.add_argument('--output', required=True)
    batch_action.add_parser('finalize').add_argument('--batch', required=True)
    batch_action.add_parser('register').add_argument('--batch', required=True)
    batch_action.add_parser('export').add_argument('--batch', required=True)
    # The earlier model-controlled tool loop remains for existing experiments.
    old = sub.add_parser('run')
    old.add_argument('--source', action='append', required=True)
    old.add_argument('--out', required=True)
    old.add_argument('--constraints', default='根据素材自主剪辑，交付可编辑草稿')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.command=='new-narration':
            from .native_narration import revise,register_revision
            result=revise(config,args.draft,args.out,args.instruction,narration_track=args.narration_track,
                caption_track=args.caption_track,resume=args.resume,name=args.name,progress=_print)
            _print({'status':result['status'],'draft':result['draft'],'voice_count':result['voice_count']})
            if args.register:_print(register_revision(config,args.out,args.name))
            return 0
        if args.command == 'native-package':
            from .native_packaging import package
            plan=json.loads(Path(args.plan).read_text(encoding='utf-8-sig'))
            result=package(config['install_dir'],args.draft,plan,args.out,
                           allow_isolated_host=args.isolated_host,user_data=config.get('user_data_dir'),resume=args.resume)
            _print(result)
            return 0 if result['status']=='native_packaging_ready_for_review' else 2
        if args.command == 'connection':
            if args.action == 'diagnose':
                from .native_diagnostics import diagnose
                result = _native_call(args,config,diagnose,with_host=True)
                _print(result)
                return 0 if result['status'] == 'transport_ready_features_unverified' else 2
            from .native_connection import check
            result = _native_call(args,config,lambda:check(config['install_dir']))
            _print(result)
            return 0 if result['status'] == 'healthy' else 2
        if args.command == 'export':
            from .native_export import export
            def native_export(host=None):
                if host is not None:
                    from .native_cli import NativeCLI
                    with NativeCLI(host) as client:
                        return export(config['install_dir'],args.draft,args.out,runner=client.run)
                return export(config['install_dir'],args.draft,args.out)
            result = _native_call(args,config,native_export,with_host=args.isolated_host)
            _print(result)
            return 0 if result['native_export_verified'] else 2
        if args.command == 'resources':
            if args.acquire and (not args.isolated_host or not args.out):
                raise ValueError('--acquire requires --isolated-host, --host-out and --out')
            if args.out and not args.acquire:
                raise ValueError('resources --out requires --acquire')
            from .resource_policy import inspect_local_catalog
            from .internal_fonts import inventory
            from .internal_resources import bridge_status, search
            from .native_connection import check as check_native_connection
            def inspect_resources(host=None):
                if (args.search or args.acquire) and host is not None:
                    from .native_cli import NativeCLI
                    with NativeCLI(host) as client:
                        if args.acquire:
                            from .native_resource_catalog import acquire
                            result = acquire(client,args.type,args.acquire,args.out)
                        else:
                            result = search(config['install_dir'], args.type, args.search, runner=client.run)
                else:
                    result = (search(config['install_dir'], args.type, args.search) if args.search else
                              inspect_local_catalog(config.get('user_data_dir')))
                result['builtin_fonts'] = inventory(config['install_dir'])
                if not args.search and not args.acquire:
                    result['bridge'] = bridge_status()
                    result['native_connection'] = check_native_connection(config['install_dir'])
                return result
            result = _native_call(args,config,inspect_resources,with_host=args.isolated_host)
            _print(result)
            return 0 if result['status'] in ('metadata_received_unverified', 'downloaded_nonmember_resource_needs_application_review') else 2
        if args.command in ('edit', 'resume', 'rerun', 'run', 'model', 'doctor', 'batch'):
            from .credential_store import credential_environment
            resources.enter_context(credential_environment(config['provider']))
        if args.command == 'doctor':
            if config.get('vision_provider'):
                from .qwen_credentials import credential_environment as qwen_credential_environment
                resources.enter_context(qwen_credential_environment(config['vision_provider']))
            result = doctor(config)
            model_dir = Path(config.get('asr_model_dir', ''))
            result['asr_model_present'] = bool(config.get('asr_model_dir') and
                                               (model_dir / 'model.int8.onnx').is_file() and
                                               (model_dir / 'tokens.txt').is_file())
            result['state_dir'] = str(Path(args.state_dir).resolve())
            draft_root = config.get('draft_root')
            result['configured_draft_root'] = draft_root
            result['configured_draft_root_exists'] = bool(draft_root and Path(draft_root).is_dir())
            user_data = config.get('user_data_dir') or str(
                Path(os.environ.get('LOCALAPPDATA', '')) / 'JianyingPro/User Data')
            result['jianying_user_data_dir'] = user_data
            try:
                result['jianying_homepage_index_exists'] = (
                    Path(user_data) / 'Projects/com.lveditor.draft/root_meta_info.json').is_file()
            except OSError:
                result['jianying_homepage_index_exists'] = None
                result['jianying_homepage_index_note'] = 'Access denied in this process; retry with filesystem access'
            result['tested_native_build'] = '11.5.0.14471'
            result['native_service_health_checked_by_doctor'] = False
            _print(result); return 0
        if args.command == 'model':
            if config.get('vision_provider'):
                from .multimodel_diagnostics import check_models
                result = check_models(config, Path(args.state_dir)/'multimodel-check-latest.json',
                    progress=lambda stage: _print({'stage': stage}))
                _print({'status':result['status'], 'providers':result['usage']['providers'],
                        'message':result.get('message'),
                        'report_path':str(Path(args.state_dir)/'multimodel-check-latest.json')})
                return 0 if result['status'] == 'passed' else 2
            from .diagnostics import check_model
            labels = {'text': '1/3：测试文本连接', 'json': '2/3：测试 JSON 输出',
                      'image': '3/3：测试图片理解'}
            if args.stage == 'image':
                labels['image'] = '仅补测图片理解'
            result = check_model(config['provider'],
                Path(args.state_dir) / 'model-check-latest.json',
                only_stage=args.stage,
                progress=lambda stage: _print({'stage': stage, 'message': labels[stage]}))
            _print(result)
            return 0 if result['status'] == 'passed' else 2
        if args.command == 'run':
            result = legacy_tool_loop(config, args.source, args.constraints, args.out)
            _print(result); return 2 if result.get('status') == 'failed' else 0
        from .task_service import TaskService, task_spec
        service = TaskService(config, args.state_dir)
        if args.command == 'batch':
            if args.batch_action == 'create':
                spec = json.loads(Path(args.spec).read_text(encoding='utf-8-sig'))
                result = service.create_batch(spec['outputs'], gap_us=args.gap_us,
                                              quality=spec.get('quality', 'standard'))
            elif args.batch_action == 'status':
                result = service.batch_status(args.batch)
            elif args.batch_action == 'retry':
                result = service.retry_batch_child(args.batch, args.output)
            elif args.batch_action == 'finalize':
                result = service.finalize_batch(args.batch)
            elif args.batch_action == 'register':
                result = service.publish_batch(args.batch)
            else:
                result = service.export_batch(args.batch)
            _print(result); return 0
        if args.command == 'rerun':
            from .evidence_reuse import rerun_with_evidence
            new_id=rerun_with_evidence(service,args.job)
            _print({'job_id':new_id,'source_job':args.job,'status':'CREATED_WITH_VERIFIED_EVIDENCE'})
            result=service.start_job(new_id)
            _print({'job_id':new_id,'state':result['state'],'error':result['error']})
            if result['state']=='READY_FOR_REVIEW':
                try:_print({'publication':service.publish_job(new_id)})
                except Exception as exc:_print({'publication':'pending','reason':str(exc),'job_id':new_id})
            return 0 if result['state']=='READY_FOR_REVIEW' else 2
        if args.command == 'edit':
            asset_ids = [service.register_asset(source) for source in args.input]
            job_id = service.create_job(task_spec(args.instruction, asset_ids, args.workflow,
                                                  target_country=args.target_country,
                                                  render_backend=config.get('render_backend_default', 'direct_ffmpeg')))
            _print({'job_id': job_id, 'asset_ids': asset_ids, 'status': 'CREATED'})
            result = service.start_job(job_id)
            _print({'job_id': job_id, 'state': result['state'], 'error': result['error'],
                    'artifacts': service.list_artifacts(job_id)})
            if result['state'] == 'READY_FOR_REVIEW' and not args.skip_publish:
                try:
                    _print({'publication': service.publish_job(job_id)})
                except Exception as exc:
                    _print({'publication': 'pending', 'job_id': job_id,
                            'reason': str(exc) if isinstance(exc, (ValueError, PermissionError))
                            else type(exc).__name__,
                            'retry_command': f'agent publish --job {job_id}'})
            return 2 if result['state'] in ('FAILED', 'WAITING_FOR_INPUT', 'BLOCKED_RESOURCES') else 0
        if args.command == 'status':
            _print(service.get_job(args.job)); return 0
        if args.command == 'cancel':
            _print(service.cancel_job(args.job)); return 0
        if args.command == 'resume':
            result = service.resume_job(args.job)
            _print({'job_id': args.job, 'state': result['state'], 'error': result['error'],
                    'artifacts': service.list_artifacts(args.job)})
            if result['state'] == 'READY_FOR_REVIEW':
                try:
                    _print({'publication': service.publish_job(args.job)})
                except Exception as exc:
                    _print({'publication': 'pending', 'job_id': args.job,
                            'reason': str(exc) if isinstance(exc, (ValueError, PermissionError))
                            else type(exc).__name__,
                            'retry_command': f'agent publish --job {args.job}'})
            return 2 if result['state'] in ('FAILED', 'WAITING_FOR_INPUT', 'BLOCKED_RESOURCES') else 0
        if args.command == 'inspect':
            _print({'job': service.get_job(args.job), 'artifacts': service.list_artifacts(args.job),
                    'events': service.read_events(args.job, 0)})
            return 0
        if args.command == 'publish':
            _print(service.publish_job(args.job)); return 0
        if args.command == 'open':
            artifacts = service.list_artifacts(args.job)
            published = next((row for row in reversed(artifacts)
                              if row['kind'] == 'publication_summary'), None)
            summary = (json.loads(Path(published['path']).read_text(encoding='utf-8-sig'))
                       if published else None)
            draft = next((row for row in reversed(artifacts)
                          if row['kind'] in ('subtitled_draft', 'native_draft')), None)
            _print({'job_id': args.job, 'draft_path':
                    summary['registered_draft_path'] if summary else
                    str(Path(draft['path']).parent) if draft else None,
                    'status': 'desktop_open_not_automated', 'jianying_open_verified': False,
                    'homepage_index_registered': bool(summary),
                    'message': '可在剪映中手动打开所列草稿；桌面目视验收尚未自动化'})
            return 2
    except Exception as exc:
        # No request bodies, headers, API keys or full provider exception traces.
        _print({'status': 'error', 'error_type': type(exc).__name__,
                'message': str(exc) if isinstance(exc, (ValueError, FileNotFoundError, KeyError, ProviderError)) else 'Inspect task events and operation logs'})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
