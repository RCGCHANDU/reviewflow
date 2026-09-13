import { Navigate, Route, Routes } from 'react-router-dom'

import { Layout } from './components/Layout'
import { Dashboard } from './routes/Dashboard'
import { NewReview } from './routes/NewReview'
import { Projects } from './routes/Projects'
import { Providers } from './routes/Providers'
import { RunDetail } from './routes/RunDetail'
import { RunHistory } from './routes/RunHistory'

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/new" element={<NewReview />} />
        <Route path="/runs" element={<RunHistory />} />
        <Route path="/runs/:runId" element={<RunDetail />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/projects/:projectId" element={<Projects />} />
        <Route path="/providers" element={<Providers />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
