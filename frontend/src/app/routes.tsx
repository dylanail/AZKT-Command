/* Route table. Pages are lazy; each screen is a default export under screens/. */
import { Suspense, lazy, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { can, isEmployeeRole } from "../lib/perms";
import { PageLoading } from "../ui";
import Shell from "./Shell";
import { routeAllowed } from "./nav";

const Home = lazy(() => import("../screens/home/Home"));
const Vehicles = lazy(() => import("../screens/vehicles/Vehicles"));
const VehicleDetail = lazy(() => import("../screens/vehicles/VehicleDetail"));
const Sales = lazy(() => import("../screens/sales/Sales"));
const ImportRequests = lazy(() => import("../screens/requests/ImportRequests"));
const ImportRequestDetail = lazy(() => import("../screens/requests/ImportRequestDetail"));
const Tasks = lazy(() => import("../screens/tasks/Tasks"));
const TaskDetail = lazy(() => import("../screens/tasks/TaskDetail"));
const Inbox = lazy(() => import("../screens/inbox/Inbox"));
const Contacts = lazy(() => import("../screens/contacts/Contacts"));
const ContactDetail = lazy(() => import("../screens/contacts/ContactDetail"));
const Agents = lazy(() => import("../screens/agents/Agents"));
const Activity = lazy(() => import("../screens/activity/Activity"));
const Finance = lazy(() => import("../screens/finance/Finance"));
const Settings = lazy(() => import("../screens/settings/Settings"));
const Team = lazy(() => import("../screens/settings/Team"));
const ApprovalReview = lazy(() => import("../screens/approvals/ApprovalReview"));
const ApprovalsList = lazy(() => import("../screens/approvals/ApprovalsList"));
const Login = lazy(() => import("../screens/auth/Login"));
const Invite = lazy(() => import("../screens/auth/Invite"));
const Denied = lazy(() => import("../screens/auth/Denied"));
const Expired = lazy(() => import("../screens/auth/Expired"));
const ShipmentDetail = lazy(() => import("../screens/shipments/ShipmentDetail"));
const ListingEditor = lazy(() => import("../screens/listings/ListingEditor"));
const CandidateDetail = lazy(() => import("../screens/candidates/CandidateDetail"));
const More = lazy(() => import("../screens/more/More"));
const NotFound = lazy(() => import("../screens/NotFound"));

function Splash() {
  return <div className="auth" aria-busy="true"><PageLoading /></div>;
}

/** Signed-in shell. Unauthenticated → /login; expired session → Expired page. */
function RequireAuth({ children }: { children: ReactNode }) {
  const { ready, authed, expired } = useAuth();
  const loc = useLocation();
  if (!ready) return <Splash />;
  if (expired) return <Suspense fallback={<Splash />}><Expired /></Suspense>;
  if (!authed) return <Navigate to="/login" replace state={{ from: loc.pathname + loc.search }} />;
  return <>{children}</>;
}

/** Role gate for a single route element. */
function Guard({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const loc = useLocation();
  if (!routeAllowed(user?.role, loc.pathname)) return <Suspense fallback={<PageLoading />}><Denied /></Suspense>;
  return <>{children}</>;
}

/** Permission gate for a single route element (e.g. the approvals queue needs `approve`). */
function PermGuard({ perm, children }: { perm: string; children: ReactNode }) {
  const { user } = useAuth();
  if (!can(user, perm)) return <Suspense fallback={<PageLoading />}><Denied /></Suspense>;
  return <>{children}</>;
}

/** Public routes bounce signed-in users into the app. */
function PublicOnly({ children }: { children: ReactNode }) {
  const { ready, authed, user } = useAuth();
  if (!ready) return <Splash />;
  if (authed) return <Navigate to={isEmployeeRole(user?.role) ? "/tasks" : "/"} replace />;
  return <>{children}</>;
}

function RoleHome() {
  const { user } = useAuth();
  if (isEmployeeRole(user?.role)) return <Navigate to="/tasks" replace />;
  return <Home />;
}

export default function AppRoutes() {
  return (
    <Suspense fallback={<Splash />}>
      <Routes>
        <Route path="/login" element={<PublicOnly><Login /></PublicOnly>} />
        <Route path="/invite/:token" element={<Invite />} />
        <Route path="/denied" element={<Denied />} />
        <Route path="/expired" element={<Expired />} />

        <Route element={<RequireAuth><Shell /></RequireAuth>}>
          <Route index element={<Guard><Suspense fallback={<PageLoading />}><RoleHome /></Suspense></Guard>} />
          <Route path="/vehicles" element={<Guard><Suspense fallback={<PageLoading title="Vehicles" />}><Vehicles /></Suspense></Guard>} />
          <Route path="/vehicles/:id" element={<Guard><Suspense fallback={<PageLoading />}><VehicleDetail /></Suspense></Guard>} />
          <Route path="/sales" element={<Guard><Suspense fallback={<PageLoading title="Sales" />}><Sales /></Suspense></Guard>} />
          <Route path="/requests" element={<Guard><Suspense fallback={<PageLoading title="Import requests" />}><ImportRequests /></Suspense></Guard>} />
          <Route path="/requests/:id" element={<Guard><Suspense fallback={<PageLoading />}><ImportRequestDetail /></Suspense></Guard>} />
          <Route path="/tasks" element={<Guard><Suspense fallback={<PageLoading title="Tasks" />}><Tasks /></Suspense></Guard>} />
          <Route path="/tasks/:id" element={<Guard><Suspense fallback={<PageLoading />}><TaskDetail /></Suspense></Guard>} />
          <Route path="/inbox" element={<Guard><Suspense fallback={<PageLoading title="Inbox" />}><Inbox /></Suspense></Guard>} />
          <Route path="/inbox/:threadId" element={<Guard><Suspense fallback={<PageLoading title="Inbox" />}><Inbox /></Suspense></Guard>} />
          <Route path="/contacts" element={<Guard><Suspense fallback={<PageLoading title="Contacts" />}><Contacts /></Suspense></Guard>} />
          <Route path="/contacts/:id" element={<Guard><Suspense fallback={<PageLoading />}><ContactDetail /></Suspense></Guard>} />
          <Route path="/agents" element={<Guard><Suspense fallback={<PageLoading title="Agents" />}><Agents /></Suspense></Guard>} />
          <Route path="/agents/:agentId" element={<Guard><Suspense fallback={<PageLoading title="Agents" />}><Agents /></Suspense></Guard>} />
          <Route path="/activity" element={<Guard><Suspense fallback={<PageLoading title="Activity" />}><Activity /></Suspense></Guard>} />
          <Route path="/finance" element={<Guard><Suspense fallback={<PageLoading title="Finance" />}><Finance /></Suspense></Guard>} />
          <Route path="/settings" element={<Guard><Suspense fallback={<PageLoading title="Settings" />}><Settings /></Suspense></Guard>} />
          <Route path="/settings/team" element={<Guard><Suspense fallback={<PageLoading title="Team" />}><Team /></Suspense></Guard>} />
          <Route path="/settings/:section" element={<Guard><Suspense fallback={<PageLoading title="Settings" />}><Settings /></Suspense></Guard>} />
          <Route path="/approvals" element={<Guard><PermGuard perm="approve"><Suspense fallback={<PageLoading title="Approvals" />}><ApprovalsList /></Suspense></PermGuard></Guard>} />
          <Route path="/approvals/:id" element={<Guard><Suspense fallback={<PageLoading />}><ApprovalReview /></Suspense></Guard>} />
          <Route path="/shipments/:id" element={<Guard><Suspense fallback={<PageLoading />}><ShipmentDetail /></Suspense></Guard>} />
          <Route path="/listings/:id" element={<Guard><Suspense fallback={<PageLoading />}><ListingEditor /></Suspense></Guard>} />
          <Route path="/candidates/:id" element={<Guard><Suspense fallback={<PageLoading />}><CandidateDetail /></Suspense></Guard>} />
          <Route path="/more" element={<Guard><Suspense fallback={<PageLoading title="More" />}><More /></Suspense></Guard>} />
          <Route path="/more/:section" element={<Guard><Suspense fallback={<PageLoading title="More" />}><More /></Suspense></Guard>} />
          <Route path="*" element={<Suspense fallback={<PageLoading />}><NotFound /></Suspense>} />
        </Route>
      </Routes>
    </Suspense>
  );
}
