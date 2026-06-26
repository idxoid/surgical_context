import * as path from 'node:path';
import * as vscode from 'vscode';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

export const LEGACY_DEFAULT_WORKSPACE_ID = 'local/default@main';

function surgicalConfig(): vscode.WorkspaceConfiguration {
  return vscode.workspace.getConfiguration('surgicalContext');
}

function normalizeConfiguredWorkspaceId(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  if (!trimmed || trimmed === LEGACY_DEFAULT_WORKSPACE_ID) return undefined;
  return trimmed;
}

export function getExplicitWorkspaceId(): string | undefined {
  const inspected = surgicalConfig().inspect<string>('workspaceId');
  return normalizeConfiguredWorkspaceId(inspected?.workspaceFolderValue)
    ?? normalizeConfiguredWorkspaceId(inspected?.workspaceValue)
    ?? normalizeConfiguredWorkspaceId(inspected?.globalValue);
}

function safeWorkspaceSegment(value: string, fallback: string): string {
  let normalized = value.trim().replace(/[^A-Za-z0-9_.-]+/g, '-');
  let start = 0;
  let end = normalized.length;
  while (start < end && normalized[start] === '-') {
    start += 1;
  }
  while (end > start && normalized[end - 1] === '-') {
    end -= 1;
  }
  if (start > 0 || end < normalized.length) {
    normalized = normalized.slice(start, end);
  }
  return normalized || fallback;
}

function safeRef(value: string | undefined): string {
  const normalized = (value || 'main').trim().replace(/\s+/g, '-');
  return normalized || 'main';
}

async function currentGitRef(folderPath: string): Promise<string | undefined> {
  try {
    const { stdout } = await execFileAsync('git', ['-C', folderPath, 'branch', '--show-current']);
    const branch = stdout.trim();
    if (branch) return branch;
  } catch {
    // Fall through to detached-head lookup.
  }

  try {
    const { stdout } = await execFileAsync('git', ['-C', folderPath, 'rev-parse', '--short', 'HEAD']);
    return stdout.trim() || undefined;
  } catch {
    return undefined;
  }
}

export async function deriveWorkspaceId(): Promise<string | undefined> {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) return undefined;

  const folderPath = folder.uri.fsPath;
  const repoName = safeWorkspaceSegment(folder.name || path.basename(folderPath), 'repo');
  const ref = safeRef(await currentGitRef(folderPath));
  return `local/${repoName}@${ref}`;
}

export async function resolveWorkspaceId(): Promise<string | undefined> {
  return getExplicitWorkspaceId() ?? await deriveWorkspaceId();
}

export function workspaceIdDisplayValue(): string {
  return getExplicitWorkspaceId() ?? '';
}
