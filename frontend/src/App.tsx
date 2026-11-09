import SummarizerLayout from "./SummarizerLayout"
import SummaryPage from "./SummaryPage"
import SidebarLayout from "./SideBarLayout"
import { Route, Routes } from "react-router-dom"

export default function App() {
  return (
    <Routes>
      <Route element={<SidebarLayout />}>
        <Route path="/" element={<SummarizerLayout />} />
        <Route path="/summary/:id" element={<SummaryPage />} />
      </Route>
    </Routes>
  )
}
