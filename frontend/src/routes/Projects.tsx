import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { EmptyState, Loading, Notice, Panel, RunStatusBadge } from '../components/Status'
import { formatBytes, formatTimestamp, relativeTime } from '../lib/format'

/** Projects and their immutable source material. */
export function Projects() {
  const { projectId } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [objective, setObjective] = useState('')
  const [uploadError, setUploadError] = useState<string | null>(null)

  const projects = useQuery({ queryKey: ['projects'], queryFn: api.projects })
  const detail = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.project(projectId as string),
    enabled: Boolean(projectId),
  })
  const samples = useQuery({ queryKey: ['samples'], queryFn: api.samples })

  const createProject = useMutation({
    mutationFn: () =>
      api.createProject({ name, description, default_objective: objective }),
    onSuccess: (project) => {
      setName('')
      setDescription('')
      setObjective('')
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
      navigate(`/projects/${project.id}`)
    },
  })

  const upload = useMutation({
    mutationFn: (file: File) => api.uploadSource(projectId as string, file),
    onSuccess: () => {
      setUploadError(null)
      void queryClient.invalidateQueries({ queryKey: ['project', projectId] })
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
    onError: (error) => setUploadError(String(error)),
  })

  const loadSample = useMutation({
    mutationFn: (sampleId: string) => api.loadSample(projectId as string, sampleId),
    onSuccess: () => {
      setUploadError(null)
      void queryClient.invalidateQueries({ queryKey: ['project', projectId] })
    },
    onError: (error) => setUploadError(String(error)),
  })

  return (
    <>
      <div className="page-head">
        <div className="grow">
          <h1>Projects</h1>
          <p>A project groups source material with the objectives you review it against.</p>
        </div>
        <Link className="button primary" to="/new">
          New review
        </Link>
      </div>

      <div className="grid cols-2">
        <Panel title="Projects">
          {projects.isLoading ? (
            <Loading />
          ) : (projects.data ?? []).length === 0 ? (
            <EmptyState title="No projects yet">Create one on the right to upload source material.</EmptyState>
          ) : (
            <div className="scroll-x">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Updated</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {projects.data?.map((project) => (
                    <tr key={project.id}>
                      <td>
                        <Link to={`/projects/${project.id}`}>{project.name}</Link>
                        {project.description ? <div className="tiny faint">{project.description}</div> : null}
                      </td>
                      <td className="small muted">{relativeTime(project.updated_at)}</td>
                      <td className="num">
                        <Link className="small" to={`/runs?project=${project.id}`}>
                          runs →
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel title="Create a project">
          <form
            onSubmit={(event) => {
              event.preventDefault()
              if (!name.trim()) return
              createProject.mutate()
            }}
          >
            <div className="field">
              <label htmlFor="project-name">Name</label>
              <input id="project-name" type="text" value={name} onChange={(event) => setName(event.target.value)} required />
            </div>
            <div className="field">
              <label htmlFor="project-description">Description</label>
              <input
                id="project-description"
                type="text"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="project-objective">Default objective</label>
              <textarea
                id="project-objective"
                value={objective}
                onChange={(event) => setObjective(event.target.value)}
                placeholder="Identify unsupported or overstated claims and fix the smallest defensible set."
              />
              <div className="help">Pre-fills the objective when you start a review from this project.</div>
            </div>
            <button className="primary" type="submit" disabled={createProject.isPending || !name.trim()}>
              {createProject.isPending ? 'Creating…' : 'Create project'}
            </button>
            {createProject.isError ? (
              <div className="tiny" style={{ color: 'var(--bad)', marginTop: 8 }}>
                {String(createProject.error)}
              </div>
            ) : null}
          </form>
        </Panel>
      </div>

      {projectId ? (
        <div style={{ marginTop: 16 }}>
          <Panel
            title={detail.data ? `Sources — ${detail.data.project.name}` : 'Sources'}
            flush
            actions={
              <Link className="small" to={`/new?project=${projectId}`}>
                Start a review →
              </Link>
            }
          >
            {detail.isLoading ? (
              <Loading />
            ) : detail.isError ? (
              <EmptyState title="Project not found">{String(detail.error)}</EmptyState>
            ) : (
              <>
                <div style={{ padding: 14, borderBottom: '1px solid var(--border)' }}>
                  <label htmlFor="source-file">Upload source material</label>
                  <div className="row">
                    <input
                      id="source-file"
                      type="file"
                      accept=".md,.markdown,.txt,.rst,.py,.json,.yaml,.yml,.csv"
                      onChange={(event) => {
                        const file = event.target.files?.[0]
                        if (file) upload.mutate(file)
                      }}
                    />
                    {upload.isPending ? <span className="small muted">Uploading…</span> : null}
                  </div>
                  <div className="help">
                    Accepted: markdown, text, reStructuredText, Python, JSON, YAML, CSV. The uploaded file is stored
                    unchanged and hashed; the engine reviews a copy.
                  </div>
                  <div className="row" style={{ marginTop: 10 }}>
                    <span className="small muted">Or load a sample:</span>
                    {(samples.data ?? []).slice(0, 4).map((sample) => (
                      <button
                        key={sample.id}
                        type="button"
                        className="small"
                        title={sample.description}
                        disabled={loadSample.isPending}
                        onClick={() => loadSample.mutate(sample.id)}
                      >
                        {sample.id}
                      </button>
                    ))}
                  </div>
                  {uploadError ? (
                    <div className="tiny" style={{ color: 'var(--bad)', marginTop: 8 }}>
                      {uploadError}
                    </div>
                  ) : null}
                </div>

                {detail.data && detail.data.sources.length === 0 ? (
                  <EmptyState title="No sources yet">Upload a document or load a sample above.</EmptyState>
                ) : (
                  <div className="scroll-x">
                    <table>
                      <thead>
                        <tr>
                          <th>Filename</th>
                          <th>Type</th>
                          <th className="num">Size</th>
                          <th>SHA-256</th>
                          <th>Added</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detail.data?.sources.map((source) => (
                          <tr key={source.id}>
                            <td>
                              {source.filename}
                              <div className="tiny faint mono">{source.id}</div>
                            </td>
                            <td className="small muted">{source.content_type}</td>
                            <td className="num">{formatBytes(source.size_bytes)}</td>
                            <td className="mono tiny" title={source.sha256}>
                              {source.sha256.slice(0, 16)}…
                            </td>
                            <td className="small muted" title={formatTimestamp(source.created_at)}>
                              {relativeTime(source.created_at)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {detail.data && detail.data.runs.length > 0 ? (
                  <div style={{ padding: 14, borderTop: '1px solid var(--border)' }}>
                    <h3 style={{ marginBottom: 8 }}>Recent runs in this project</h3>
                    <div className="scroll-x">
                      <table>
                        <thead>
                          <tr>
                            <th>Run</th>
                            <th>Objective</th>
                            <th>Status</th>
                            <th>Started</th>
                          </tr>
                        </thead>
                        <tbody>
                          {detail.data.runs.map((run) => (
                            <tr key={run.id}>
                              <td>
                                <Link to={`/runs/${run.id}`} className="mono">
                                  {run.id.slice(0, 12)}
                                </Link>
                              </td>
                              <td className="small">{run.objective.slice(0, 80)}</td>
                              <td>
                                <RunStatusBadge status={run.status} />
                              </td>
                              <td className="small muted">{relativeTime(run.started_at ?? run.created_at)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                ) : null}
              </>
            )}
          </Panel>
        </div>
      ) : null}

      {!projectId ? (
        <Notice>
          Select a project above to upload source material and see its runs.
        </Notice>
      ) : null}
    </>
  )
}
