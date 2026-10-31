import type React from "react";
import { useEffect, useState } from "react";
import { Toaster } from "sonner";
import { ChatPage } from "@/pages/ChatPage";
import { CostPage } from "@/pages/CostPage";

/** Minimal hash routing.
 *
 *  The app is one page plus the cost forecast, so a hash check beats adding react-router:
 *  one fewer dependency to resolve, which matters now that the container runs `npm install`
 *  on every boot. The SPA fallback in server/app.py already serves index.html for unknown
 *  paths, so `#/cost` needs no server route.
 */
function useHashRoute(): string {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash.replace(/^#\/?/, "");
}

/** Table de routage : une entrée par écran, le chat servant de défaut. Préférée à
 *  des ternaires imbriqués, illisibles dès le troisième écran. */
const ROUTES: Record<string, React.ReactElement> = {
  cost: <CostPage />,
};

export default function App() {
  const route = useHashRoute();
  return (
    <>
      <Toaster position="top-right" richColors />
      {/* Le sélecteur de thème n'est PLUS en surimpression. Il l'était pour
          qu'aucun écran n'ait à l'accueillir — mais les quatre coins sont
          occupés : les toasts en haut à droite, le logo en haut à gauche, le
          lien « Cost forecast » en bas à gauche, le bouton d'envoi en bas à
          droite. En `fixed bottom-3 right-3 z-50` il recouvrait donc le bouton
          d'envoi dès que la fenêtre descendait sous ~1200px de large, sans que
          rien ne le signale. Chaque écran l'accueille désormais dans son flux :
          la collision disparaît au lieu d'être déplacée. */}
      {ROUTES[route] ?? <ChatPage />}
    </>
  );
}
