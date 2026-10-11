import assert from 'node:assert/strict';
import test from 'node:test';
import { taskArtifactArchiveUrl, taskArtifactDownloadUrl } from '../modules/api_client.js';

test('captured task files use the backend canonical URL encoding', () => {
    assert.equal(taskArtifactDownloadUrl('task', "résumé's (complete).zip"),
        '/api/tasks/task/artifacts/r%C3%A9sum%C3%A9%27s%20%28complete%29.zip');
    assert.equal(taskArtifactDownloadUrl('task', 'ordinary.bin'), '/api/tasks/task/artifacts/ordinary.bin');
    assert.equal(taskArtifactDownloadUrl('task', 'report.pdf', 'nested/a/report.pdf'),
        '/api/tasks/task/artifacts/report.pdf?relpath=nested%2Fa%2Freport.pdf');
    for (const relpath of ['../report.pdf', '/report.pdf', 'nested/../report.pdf',
        'nested//report.pdf', 'nested\\report.pdf', 'nested/other.pdf']) {
        assert.equal(taskArtifactDownloadUrl('task', 'report.pdf', relpath), '');
    }
    for (const name of ['../other', 'folder/file', 'folder\\file', '.artifact_manifest.json']) {
        assert.equal(taskArtifactDownloadUrl('task', name), '');
    }
    assert.equal(taskArtifactDownloadUrl('other/task', 'file.bin'), '');
});

test('a recorded directory archive is addressed by its basename plus .zip and the exact directory', () => {
    assert.equal(taskArtifactArchiveUrl('task', 'repo'), '/api/tasks/task/artifacts/repo.zip?archive=repo');
    assert.equal(taskArtifactArchiveUrl('task', "nested/src dir's"),
        "/api/tasks/task/artifacts/src%20dir%27s.zip?archive=nested%2Fsrc%20dir's");
    for (const directory of ['', '../repo', '/repo', 'repo/', 'repo/../x', 'repo\\src', '.', 'a/./b', 'a\0b',
        42, null, undefined]) {
        assert.equal(taskArtifactArchiveUrl('task', directory), '', JSON.stringify(directory));
    }
    assert.equal(taskArtifactArchiveUrl('task', '.github'), '/api/tasks/task/artifacts/.github.zip?archive=.github');
    assert.equal(taskArtifactArchiveUrl('other/task', 'repo'), '');
});
