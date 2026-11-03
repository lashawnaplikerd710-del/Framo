"""Hidden local credential setup and launcher. No command prints the API key."""
import argparse
import json
from pathlib import Path
import sys

from .credential_store import (CredentialError, credential_environment, delete_key,
                               load_key, prompt_and_save, save_key, validate_key, TARGET)


def save_from_pipe(stream):
    """Local password dialog transport. Never echo input or accept argv secrets."""
    value = stream.read(1025)
    try:
        value = value.rstrip('\r\n')
        save_key(validate_key(value))
    finally:
        value = None
    return {'saved': True, 'store': 'Windows Credential Manager'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('save', 'save-stdin', 'status', 'delete', 'run', 'check'))
    root = Path(__file__).resolve().parents[1]
    parser.add_argument('--config', default=str(root/'config.native.json'))
    parser.add_argument('--state-dir', default=str(root/'data'))
    parser.add_argument('--resume-job')
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--input', action='append')
    parser.add_argument('--instruction')
    parser.add_argument('--workflow', default='talking_head')
    parser.add_argument('--stage', choices=('all', 'image'), default='all')
    args = parser.parse_args()
    try:
        if args.action == 'save-stdin':
            if sys.stdin.isatty():
                raise CredentialError('Use the local password dialog or the hidden save prompt.')
            print(json.dumps(save_from_pipe(sys.stdin)))
            return 0
        if args.action == 'save':
            prompt_and_save()
            return 0
        if args.action == 'status':
            print(json.dumps({'saved': load_key() is not None, 'store': 'Windows Credential Manager',
                              'target': TARGET, 'scope': 'same_user_same_computer'}))
            return 0
        if args.action == 'delete':
            print(json.dumps({'removed': delete_key(), 'target': TARGET}))
            return 0
        from .runtime import load_config
        config = load_config(args.config)
        if args.action == 'run' and not args.resume_job and (not args.input or not args.instruction):
            raise ValueError('Specify resume-job, or input and instruction.')
        if args.background and not args.resume_job:
            raise ValueError('Background launch currently requires an existing job.')
        with credential_environment(config['provider'], interactive=True):
            if args.background:
                from .background import launch
                result = launch(args.config, args.state_dir, args.resume_job)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            from .__main__ import main as cli
            saved_argv = sys.argv
            sys.argv = ['agent', '--config', args.config, '--state-dir', args.state_dir]
            if args.action == 'check':
                sys.argv += ['model', 'check', '--stage', args.stage]
            elif args.resume_job:
                sys.argv += ['resume', '--job', args.resume_job]
            else:
                sys.argv += ['edit', '--instruction', args.instruction, '--workflow', args.workflow]
                for source in args.input:
                    sys.argv += ['--input', source]
            try:
                return cli()
            finally:
                sys.argv = saved_argv
    except (CredentialError, ValueError, FileNotFoundError) as exc:
        print(json.dumps({'status': 'error', 'message': str(exc)}, ensure_ascii=False))
        return 2
    except (KeyboardInterrupt, EOFError):
        print('Credential input cancelled; no key is printed.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
