import { BrowserRouter } from "react-router-dom";
import { AuthProvider } from "./lib/auth";
import { ThemeProvider } from "./lib/theme";
import { ToastProvider } from "./ui/Toast";
import { InspectorProvider } from "./app/Inspector";
import AppRoutes from "./app/routes";

export default function App() {
  return (
    <ThemeProvider>
      <ToastProvider>
        <BrowserRouter>
          <AuthProvider>
            <InspectorProvider>
              <AppRoutes />
            </InspectorProvider>
          </AuthProvider>
        </BrowserRouter>
      </ToastProvider>
    </ThemeProvider>
  );
}
