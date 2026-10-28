# Lernskript Kurvendiskussion

Dieses Skript fasst zusammen, was Sie für eine Kurvendiskussion brauchen: Regeln, Bedingungen und Verfahren, jeweils mit einem durchgerechneten Beispiel. Es behandelt ganzrationale Funktionen vom Grad 2 bis 4 und im Kapitel zu Asymptoten einfache gebrochenrationale Funktionen.

## 1. Grundlagen: Funktionsterm, Definitionsbereich, Symmetrie

### Funktionsterm einer ganzrationalen Funktion

Eine ganzrationale Funktion (Polynomfunktion) hat einen Term der Form $f(x)=a_n x^n + a_{n-1} x^{n-1} + ... + a_1 x + a_0$ mit reellen Koeffizienten und $a_n \ne 0$. Die höchste Potenz $n$ heißt Grad der Funktion, $a_n$ heißt Leitkoeffizient. So hat $f(x)=2x^3-3x^2-12x+5$ den Grad $3$ und den Leitkoeffizienten $2$.

### Definitionsbereich

Der Definitionsbereich enthält alle $x$-Werte, die Sie in den Funktionsterm einsetzen dürfen. Bei ganzrationalen Funktionen ist das immer ganz $\mathbb{R}$. Bei gebrochenrationalen Funktionen müssen Sie alle $x$-Werte ausschließen, für die der Nenner null wird.

### Achsensymmetrie zur y-Achse

Der Graph von $f$ ist achsensymmetrisch zur $y$-Achse, wenn für alle $x$ gilt: $f(-x)=f(x)$. Bei einer ganzrationalen Funktion ist das genau dann der Fall, wenn im Term nur gerade Exponenten vorkommen; ein konstantes Glied zählt dabei als $x^0$, also als gerade.

### Punktsymmetrie zum Ursprung

Der Graph von $f$ ist punktsymmetrisch zum Ursprung, wenn für alle $x$ gilt: $f(-x)=-f(x)$. Bei einer ganzrationalen Funktion ist das genau dann der Fall, wenn im Term nur ungerade Exponenten vorkommen; ein konstantes Glied ungleich null verhindert diese Symmetrie.

### Beispiel: Symmetrie prüfen

Für $f(x)=x^4-2x^2+1$ gilt $f(-x)=(-x)^4-2(-x)^2+1=x^4-2x^2+1=f(x)$, der Graph ist also achsensymmetrisch zur $y$-Achse. Für $g(x)=x^3-4x$ gilt $g(-x)=-x^3+4x=-g(x)$, der Graph ist punktsymmetrisch zum Ursprung. Für $h(x)=x^3+x^2$ ist $h(1)=2$, aber $h(-1)=0$; damit liegt keine der beiden Symmetrien vor.

### Häufiger Fehler bei der Symmetrie

Ein konstantes Glied wird oft übersehen: $k(x)=x^3+1$ ist nicht punktsymmetrisch zum Ursprung, denn $k(-x)+k(x)=2$ statt $0$. Prüfen Sie Symmetrie deshalb immer am vollständigen Term.

## 2. Ableiten: Regeln, f′ und f″ als Werkzeug

### Potenzregel

Für $f(x)=x^n$ gilt $f'(x)=n \cdot x^{n-1}$. Der Exponent wandert als Faktor nach vorne, und der neue Exponent ist um eins kleiner.

### Faktor-, Summen- und Konstantenregel

Ein konstanter Faktor bleibt beim Ableiten erhalten: Aus $f(x)=c \cdot g(x)$ folgt $f'(x)=c \cdot g'(x)$. Eine Summe wird gliedweise abgeleitet. Ein konstantes Glied fällt beim Ableiten weg, weil die Ableitung einer Konstanten null ist.

### Quotientenregel

Für $f(x)=\frac{u(x)}{v(x)}$ mit $v(x) \ne 0$ gilt $f'(x)=\frac{u'(x)v(x)-u(x)v'(x)}{(v(x))^2}$. Sie brauchen diese Regel für gebrochenrationale Funktionen.

### Bedeutung der Ableitungen

Der Wert $f'(x_0)$ ist die Steigung des Graphen von $f$ an der Stelle $x_0$, also die Steigung der Tangente dort. Die zweite Ableitung $f''$ beschreibt, wie sich die Steigung ändert, und damit die Krümmung des Graphen. Die dritte Ableitung $f'''$ brauchen Sie als hinreichende Bedingung für Wendepunkte.

### Beispiel: Ableitungen bilden

Für $f(x)=2x^3-3x^2-12x+5$ erhalten Sie gliedweise $f'(x)=6x^2-6x-12$, dann $f''(x)=12x-6$ und $f'''(x)=12$. An der Stelle $x_0=3$ hat der Graph die Steigung $f'(3)=54-18-12=24$, an der Stelle $x_0=0$ die Steigung $f'(0)=-12$.

### Beispiel: Quotientenregel

Für $f(x)=\frac{2x+1}{x-1}$ ist $u(x)=2x+1$, $u'(x)=2$, $v(x)=x-1$ und $v'(x)=1$. Damit gilt $f'(x)=\frac{2(x-1)-(2x+1)}{(x-1)^2}=\frac{-3}{(x-1)^2}$.

## 3. Nullstellen und Schnittpunkte

### Nullstellen und Schnittpunkt mit der y-Achse

Nullstellen sind die Lösungen der Gleichung $f(x)=0$; dort schneidet oder berührt der Graph die $x$-Achse. Den Schnittpunkt mit der $y$-Achse erhalten Sie, indem Sie $x=0$ einsetzen: $S_y(0|f(0))$.

### Verfahren: Ausklammern

Fehlt im Term das konstante Glied, klammern Sie eine Potenz von $x$ aus. Ein Produkt ist genau dann null, wenn mindestens ein Faktor null ist (Satz vom Nullprodukt).

### Verfahren: Mitternachtsformel und pq-Formel

Eine quadratische Gleichung $ax^2+bx+c=0$ mit $a \ne 0$ hat die Lösungen $x_{1,2}=\frac{-b \pm \sqrt{b^2-4ac}}{2a}$, sofern $b^2-4ac \ge 0$. In der Normalform $x^2+px+q=0$ gilt $x_{1,2}=-\frac{p}{2} \pm \sqrt{\left(\frac{p}{2}\right)^2-q}$. Ist der Ausdruck unter der Wurzel negativ, gibt es keine reelle Lösung.

### Verfahren: Substitution

Kommen nur die Potenzen $x^4$ und $x^2$ (und eine Konstante) vor, setzen Sie $z=x^2$ und lösen die entstehende quadratische Gleichung in $z$. Danach ziehen Sie aus jeder Lösung $z \ge 0$ die Wurzel: $x=\pm\sqrt{z}$. Negative Lösungen für $z$ liefern keine Nullstellen.

### Verfahren: Polynomdivision

Bei einer Funktion dritten Grades ohne Ausklammermöglichkeit raten Sie eine Nullstelle $x_1$, meist unter den Teilern des konstanten Glieds. Dann teilen Sie den Term durch $(x-x_1)$ und lösen die verbleibende quadratische Gleichung.

### Vielfachheit einer Nullstelle

Kommt der Faktor $(x-x_1)$ genau einmal vor, schneidet der Graph die $x$-Achse dort mit Vorzeichenwechsel. Kommt er doppelt vor, wie in $(x-2)^2$, berührt der Graph die $x$-Achse nur; an einer doppelten Nullstelle liegt deshalb immer ein Extrempunkt. Bei einer dreifachen Nullstelle wie in $(x-2)^3$ schneidet der Graph die $x$-Achse mit waagerechter Tangente, dort liegt ein Sattelpunkt.

### Schnittpunkte zweier Graphen

Für die Schnittpunkte der Graphen von $f$ und $g$ setzen Sie die Terme gleich: $f(x)=g(x)$. Die Lösungen sind die $x$-Koordinaten der Schnittpunkte; die $y$-Koordinaten erhalten Sie durch Einsetzen in $f$ oder $g$.

### Beispiel: Ausklammern

Für $f(x)=x^3-x^2-6x$ gilt $f(x)=x(x^2-x-6)=x(x-3)(x+2)$. Die Nullstellen sind $x_1=-2$, $x_2=0$ und $x_3=3$.

### Beispiel: Substitution

Für $f(x)=x^4-5x^2+4$ setzen Sie $z=x^2$ und erhalten $z^2-5z+4=0$ mit den Lösungen $z_1=1$ und $z_2=4$. Daraus folgen die vier Nullstellen $x=-2$, $x=-1$, $x=1$ und $x=2$.

### Beispiel: Polynomdivision

Für $f(x)=x^3-2x^2-5x+6$ ist $f(1)=1-2-5+6=0$, also ist $x_1=1$ eine Nullstelle. Die Division $(x^3-2x^2-5x+6):(x-1)$ ergibt $x^2-x-6$ ohne Rest, und daraus folgen $x_2=-2$ und $x_3=3$. Der Graph schneidet die $y$-Achse in $S_y(0|6)$.

### Beispiel: Schnittpunkte zweier Graphen

Gegeben sind $f(x)=x^2-2x$ und $g(x)=x+4$. Aus $x^2-2x=x+4$ folgt $x^2-3x-4=0$, und die pq-Formel liefert $x=\frac{3}{2} \pm \frac{5}{2}$, also $x_1=-1$ und $x_2=4$. Einsetzen ergibt die Schnittpunkte $S_1(-1|3)$ und $S_2(4|8)$.

### Häufige Fehler bei Nullstellen

Wer in $x^3-x^2-6x=0$ durch $x$ teilt, statt auszuklammern, verliert die Nullstelle $x=0$. Bei Schnittpunkten gehört zur Antwort immer auch die $y$-Koordinate; ein $x$-Wert allein ist noch kein Punkt.

## 4. Monotonie und Extrempunkte

### Monotoniekriterium

Gilt $f'(x) > 0$ für alle $x$ eines Intervalls, ist $f$ dort streng monoton steigend. Gilt $f'(x) < 0$ für alle $x$ eines Intervalls, ist $f$ dort streng monoton fallend. Die Grenzen der Monotoniebereiche liegen bei ganzrationalen Funktionen an Stellen mit $f'(x)=0$.

### Hochpunkt, Tiefpunkt, lokal und global

Ein Hochpunkt hat einen größeren Funktionswert als alle Punkte in seiner Umgebung, ein Tiefpunkt einen kleineren; beide heißen Extrempunkte. Ein lokales Extremum gilt nur für die Umgebung; ist der Wert der größte oder kleinste des gesamten Definitionsbereichs, spricht man von einem globalen Extremum.

### Notwendige Bedingung für Extremstellen

Hat $f$ an der Stelle $x_0$ eine Extremstelle, dann gilt $f'(x_0)=0$; der Graph hat dort eine waagerechte Tangente. Diese Bedingung ist notwendig, aber nicht hinreichend: Nicht jede Stelle mit $f'(x_0)=0$ ist eine Extremstelle.

### Hinreichende Bedingung mit der zweiten Ableitung

Gilt $f'(x_0)=0$ und $f''(x_0) < 0$, hat $f$ bei $x_0$ einen Hochpunkt. Gilt $f'(x_0)=0$ und $f''(x_0) > 0$, hat $f$ bei $x_0$ einen Tiefpunkt. Ist $f''(x_0)=0$, liefert dieses Kriterium keine Entscheidung.

### Hinreichende Bedingung mit dem Vorzeichenwechsel

Wechselt $f'$ an der Stelle $x_0$ das Vorzeichen von plus nach minus, liegt ein Hochpunkt vor; von minus nach plus ein Tiefpunkt. Wechselt $f'$ das Vorzeichen nicht, liegt kein Extrempunkt vor. Dieses Kriterium hilft auch dann, wenn $f''(x_0)=0$ ist.

### Verfahren: Extrempunkte bestimmen

Bilden Sie $f'$ und $f''$, lösen Sie $f'(x)=0$ und prüfen Sie jede Lösung mit $f''$ oder dem Vorzeichenwechsel. Die $y$-Koordinate jedes Extrempunkts berechnen Sie durch Einsetzen in die Ausgangsfunktion $f$.

### Beispiel: Extrempunkte und Monotonie

Für $f(x)=2x^3-3x^2-12x+5$ gilt $f'(x)=6x^2-6x-12=6(x-2)(x+1)$ mit den Nullstellen $x_1=-1$ und $x_2=2$. Wegen $f''(-1)=-18 < 0$ und $f(-1)=12$ liegt der Hochpunkt $H(-1|12)$ vor; wegen $f''(2)=18 > 0$ und $f(2)=-15$ der Tiefpunkt $T(2|-15)$. Der Graph steigt für $x \le -1$, fällt für $-1 \le x \le 2$ und steigt für $x \ge 2$; das bestätigen etwa $f'(-2)=24$, $f'(0)=-12$ und $f'(3)=24$.

### Häufiger Fehler: f′(x) = 0 allein reicht nicht

Für $f(x)=x^3$ gilt $f'(0)=0$, aber $f'(x)=3x^2$ ist links und rechts von $0$ positiv; es gibt keinen Vorzeichenwechsel und damit keinen Extrempunkt, sondern einen Sattelpunkt. Umgekehrt gilt für $f(x)=x^4$ zwar $f''(0)=0$, trotzdem hat der Graph bei $x=0$ einen Tiefpunkt, denn $f'$ wechselt dort von minus nach plus.

## 5. Krümmung und Wendepunkte

### Krümmungsverhalten

Gilt $f''(x) > 0$ auf einem Intervall, ist der Graph dort linksgekrümmt (die Steigung nimmt zu). Gilt $f''(x) < 0$, ist der Graph dort rechtsgekrümmt (die Steigung nimmt ab).

### Wendepunkt

Ein Wendepunkt ist ein Punkt, an dem der Graph sein Krümmungsverhalten ändert, also von links- in rechtsgekrümmt übergeht oder umgekehrt. An einem Wendepunkt ist die Steigung lokal am größten oder am kleinsten.

### Notwendige und hinreichende Bedingung für Wendestellen

Notwendig für eine Wendestelle $x_0$ ist $f''(x_0)=0$. Hinreichend ist $f''(x_0)=0$ zusammen mit $f'''(x_0) \ne 0$, oder ein Vorzeichenwechsel von $f''$ an der Stelle $x_0$. Die $y$-Koordinate berechnen Sie wieder mit $f$.

### Sattelpunkt

Ein Sattelpunkt (auch Terrassenpunkt) ist ein Wendepunkt mit waagerechter Tangente: Dort gilt zusätzlich $f'(x_0)=0$. Ein Sattelpunkt ist kein Extrempunkt.

### Wendetangente

Die Tangente im Wendepunkt heißt Wendetangente. Sie berechnen sie wie jede Tangente mit der Formel aus Kapitel 6.

### Beispiel: Wendepunkt einer Funktion dritten Grades

Für $f(x)=2x^3-3x^2-12x+5$ gilt $f''(x)=12x-6=0$ genau für $x_0=\frac{1}{2}$, und wegen $f'''(x)=12 \ne 0$ ist das eine Wendestelle. Mit $f\left(\frac{1}{2}\right)=-\frac{3}{2}$ ergibt sich $W\left(\frac{1}{2}|-\frac{3}{2}\right)$; der Graph ist für $x < \frac{1}{2}$ rechtsgekrümmt und für $x > \frac{1}{2}$ linksgekrümmt. Die Wendetangente hat die Steigung $f'\left(\frac{1}{2}\right)=-\frac{27}{2}$ und lautet $t(x)=-\frac{27}{2}x+\frac{21}{4}$.

### Beispiel: Sattelpunkt

Für $f(x)=x^3-3x^2+3x$ gilt $f'(x)=3(x-1)^2$, $f''(x)=6x-6$ und $f'''(x)=6$. An der Stelle $x_0=1$ gilt $f'(1)=0$, $f''(1)=0$ und $f'''(1)=6 \ne 0$; mit $f(1)=1$ ist $S(1|1)$ ein Sattelpunkt.

### Beispiel: Wendepunkte einer Funktion vierten Grades

Für $f(x)=x^4-6x^2$ gilt $f''(x)=12x^2-12$ mit den Nullstellen $x=-1$ und $x=1$. Wegen $f'''(x)=24x$ ist $f'''(1)=24 \ne 0$ und $f'''(-1)=-24 \ne 0$. Mit $f(\pm 1)=-5$ ergeben sich die Wendepunkte $W_1(-1|-5)$ und $W_2(1|-5)$, passend zur Achsensymmetrie des Graphen.

### Häufiger Fehler: f″(x) = 0 allein reicht nicht

Für $f(x)=x^4$ gilt $f''(x)=12x^2$ und damit $f''(0)=0$, aber $f''$ ist auf beiden Seiten von $0$ positiv. Der Graph bleibt linksgekrümmt, und bei $x=0$ liegt kein Wendepunkt, sondern ein Tiefpunkt.

## 6. Tangente und Normale

### Tangentengleichung

Die Tangente an den Graphen von $f$ im Punkt $P(x_0|f(x_0))$ ist die Gerade durch $P$ mit der Steigung $f'(x_0)$:

$$t(x)=f'(x_0)\cdot(x-x_0)+f(x_0)$$

### Normalengleichung

Die Normale im Punkt $P(x_0|f(x_0))$ steht senkrecht auf der Tangente. Für $f'(x_0) \ne 0$ hat sie die Steigung $-\frac{1}{f'(x_0)}$, denn die Steigungen zweier senkrechter Geraden haben das Produkt $-1$:

$$n(x)=-\frac{1}{f'(x_0)}\cdot(x-x_0)+f(x_0)$$

Ist $f'(x_0)=0$, ist die Tangente waagerecht und die Normale die senkrechte Gerade $x=x_0$.

### Beispiel: Tangente und Normale in einem Punkt

Für $f(x)=x^2-4x+5$ und $x_0=3$ gilt $f(3)=2$ und $f'(x)=2x-4$, also $f'(3)=2$. Die Tangente lautet $t(x)=2(x-3)+2=2x-4$, die Normale $n(x)=-\frac{1}{2}(x-3)+2=-\frac{1}{2}x+\frac{7}{2}$. Bei $x=2$ hat dieser Graph wegen $f'(2)=0$ eine waagerechte Tangente durch den Punkt $(2|1)$.

### Tangente mit vorgegebener Steigung

Soll die Tangente die Steigung $m$ haben, lösen Sie $f'(x)=m$ und setzen jede Lösung als $x_0$ in die Tangentengleichung ein. Für $f(x)=x^2$ und $m=4$ folgt aus $2x=4$ die Stelle $x_0=2$ und die Tangente $t(x)=4x-4$.

### Tangente von einem Punkt außerhalb des Graphen

Liegt der Punkt $P(p|q)$ nicht auf dem Graphen, ist die Berührstelle $u$ unbekannt. Stellen Sie die Tangente an der Stelle $u$ auf, $t(x)=f'(u)(x-u)+f(u)$, und fordern Sie, dass $P$ auf ihr liegt: $q=f'(u)(p-u)+f(u)$. Die Lösungen dieser Gleichung für $u$ sind die Berührstellen; jede liefert eine Tangente.

### Beispiel: Tangenten von einem Punkt außerhalb

Gesucht sind die Tangenten an $f(x)=x^2$ durch $P(1|-3)$; wegen $f(1)=1$ liegt $P$ nicht auf dem Graphen. Aus $-3=2u(1-u)+u^2=2u-u^2$ folgt $u^2-2u-3=0$, also $u_1=-1$ und $u_2=3$. Die Tangenten lauten $t_1(x)=-2(x+1)+1=-2x-1$ und $t_2(x)=6(x-3)+9=6x-9$; beide gehen durch $P$, denn $t_1(1)=t_2(1)=-3$.

### Häufige Fehler bei Tangente und Normale

Die Steigung der Tangente ist $f'(x_0)$, nicht $f(x_0)$. Die Steigung der Normalen ist der negative Kehrwert $-\frac{1}{f'(x_0)}$, nicht einfach $-f'(x_0)$.

## 7. Verhalten im Unendlichen, Asymptoten

### Verhalten ganzrationaler Funktionen im Unendlichen

Für $x \to \infty$ und $x \to -\infty$ bestimmt allein der Summand mit der höchsten Potenz $a_n x^n$ das Verhalten. Bei geradem Grad laufen beide Enden in dieselbe Richtung, bei ungeradem Grad in entgegengesetzte Richtungen; das Vorzeichen von $a_n$ legt fest, welche. Für $f(x)=-2x^3+x$ gilt $\lim_{x \to \infty} f(x)=-\infty$ und $\lim_{x \to -\infty} f(x)=\infty$.

### Gebrochenrationale Funktion und Definitionslücke

Eine gebrochenrationale Funktion hat die Form $f(x)=\frac{p(x)}{q(x)}$ mit Polynomen $p$ und $q$. Jede Nullstelle des Nenners $q$ ist eine Definitionslücke und gehört nicht zum Definitionsbereich.

### Polstelle und senkrechte Asymptote

Ist $x_0$ eine Nullstelle des Nenners, aber nicht des Zählers, ist $x_0$ eine Polstelle: Die Funktionswerte wachsen bei Annäherung an $x_0$ betragsmäßig über alle Grenzen. Die senkrechte Gerade $x=x_0$ ist dann eine senkrechte Asymptote. Wechseln die Funktionswerte beim Durchgang durch $x_0$ das Vorzeichen, spricht man von einer Polstelle mit Vorzeichenwechsel; bei $f(x)=\frac{1}{x^2}$ gehen beide Seiten von $0$ gegen $+\infty$, also ohne Vorzeichenwechsel.

### Hebbare Definitionslücke

Sind Zähler und Nenner an der Stelle $x_0$ beide null, kürzen Sie den gemeinsamen Faktor $(x-x_0)$. Ist der Nenner des gekürzten Terms bei $x_0$ ungleich null, ist die Lücke hebbar und der Graph hat dort nur ein Loch; andernfalls ist $x_0$ trotzdem eine Polstelle. So gilt $\frac{x^2-1}{x-1}=x+1$ für $x \ne 1$, und der Graph ist die Gerade $y=x+1$ ohne den Punkt $(1|2)$.

### Waagerechte Asymptote

Ist der Zählergrad kleiner als der Nennergrad, ist $y=0$ waagerechte Asymptote, zum Beispiel bei $\frac{x+5}{x^2+1}$. Sind Zählergrad und Nennergrad gleich, ist $y=\frac{a}{b}$ waagerechte Asymptote, wobei $a$ und $b$ die Leitkoeffizienten von Zähler und Nenner sind; für $\frac{3x^2-1}{x^2+2}$ ist das $y=3$.

### Schiefe Asymptote

Ist der Zählergrad um genau eins größer als der Nennergrad, gibt es eine schiefe Asymptote. Sie finden sie per Polynomdivision: Der ganzrationale Anteil $mx+b$ ist die Asymptote, weil der Restbruch für $x \to \pm\infty$ gegen null geht. Ist der Zählergrad um mehr als eins größer, gibt es keine Gerade als Asymptote.

### Beispiel: Polstelle und waagerechte Asymptote

Für $f(x)=\frac{2x+1}{x-1}$ ist der Definitionsbereich ganz $\mathbb{R}$ außer $x=1$. Der Zähler hat bei $1$ den Wert $3 \ne 0$, also ist $x=1$ Polstelle mit Vorzeichenwechsel: Für $x \to 1$ von rechts gilt $f(x) \to \infty$, von links $f(x) \to -\infty$. Wegen gleicher Grade ist $y=2$ waagerechte Asymptote, was auch die Zerlegung $f(x)=2+\frac{3}{x-1}$ zeigt.

### Beispiel: weitere Eigenschaften derselben Funktion

Die Nullstelle von $f(x)=\frac{2x+1}{x-1}$ ist die Nullstelle des Zählers, $x=-\frac{1}{2}$, und der Graph schneidet die $y$-Achse bei $f(0)=-1$. Wegen $f'(x)=\frac{-3}{(x-1)^2} < 0$ für alle $x \ne 1$ hat der Graph keine Extrempunkte.

### Beispiel: schiefe Asymptote

Für $f(x)=\frac{x^2+x-1}{x-1}$ liefert die Polynomdivision $f(x)=x+2+\frac{1}{x-1}$. Die Gerade $y=x+2$ ist schiefe Asymptote, und weil der Zähler bei $x=1$ den Wert $1 \ne 0$ hat, ist $x=1$ eine senkrechte Asymptote.

### Häufiger Fehler: nicht jede Nennernullstelle ist eine Polstelle

Eine Nullstelle des Nenners ist erst dann sicher eine Polstelle, wenn Sie geprüft haben, dass sie nach dem Kürzen nicht verschwindet. Bei $\frac{x^2-1}{x-1}$ liegt bei $x=1$ keine Polstelle, sondern eine hebbare Lücke.

## 8. Komplette Kurvendiskussion

### Ablauf einer Kurvendiskussion

Eine vollständige Kurvendiskussion geht in dieser Reihenfolge vor: Definitionsbereich, Symmetrie, Verhalten im Unendlichen, Schnittpunkte mit den Achsen, Ableitungen, Extrempunkte, Wendepunkte, Monotonie und Krümmung, zum Schluss eine Skizze. Jeder Schritt verwendet die Verfahren aus den Kapiteln 1 bis 7.

### Schritt 1: Definitionsbereich, Symmetrie, Verhalten im Unendlichen

Untersucht wird $f(x)=x^3-3x^2+4$. Als ganzrationale Funktion ist $f$ auf ganz $\mathbb{R}$ definiert. Wegen $f(-x)=-x^3-3x^2+4$ ist weder $f(-x)=f(x)$ noch $f(-x)=-f(x)$, der Graph hat also keine Symmetrie zur $y$-Achse oder zum Ursprung. Wegen des Leitterms $x^3$ gilt $\lim_{x \to \infty} f(x)=\infty$ und $\lim_{x \to -\infty} f(x)=-\infty$.

### Schritt 2: Schnittpunkte mit den Achsen

Wegen $f(0)=4$ schneidet der Graph die $y$-Achse in $S_y(0|4)$. Durch Probieren finden Sie $f(-1)=-1-3+4=0$; die Polynomdivision durch $(x+1)$ ergibt $x^2-4x+4=(x-2)^2$. Also ist $f(x)=(x+1)(x-2)^2$ mit der einfachen Nullstelle $x_1=-1$ und der doppelten Nullstelle $x_2=2$, an der der Graph die $x$-Achse berührt.

### Schritt 3: Ableitungen

Es gilt $f'(x)=3x^2-6x$, $f''(x)=6x-6$ und $f'''(x)=6$.

### Schritt 4: Extrempunkte

Aus $f'(x)=3x(x-2)=0$ folgen $x=0$ und $x=2$. Wegen $f''(0)=-6 < 0$ und $f(0)=4$ ist $H(0|4)$ ein Hochpunkt; wegen $f''(2)=6 > 0$ und $f(2)=0$ ist $T(2|0)$ ein Tiefpunkt. Der Tiefpunkt liegt auf der $x$-Achse, wie es die doppelte Nullstelle erwarten lässt.

### Schritt 5: Wendepunkt und Wendetangente

Aus $f''(x)=6x-6=0$ folgt $x=1$, und wegen $f'''(1)=6 \ne 0$ ist das eine Wendestelle. Mit $f(1)=2$ ergibt sich $W(1|2)$; der Graph ist punktsymmetrisch zu diesem Wendepunkt. Die Wendetangente hat die Steigung $f'(1)=-3$ und lautet $t(x)=-3(x-1)+2=-3x+5$.

### Schritt 6: Monotonie und Krümmung

Der Graph steigt für $x \le 0$, fällt für $0 \le x \le 2$ und steigt für $x \ge 2$; Stichproben sind $f'(-1)=9$, $f'(1)=-3$ und $f'(3)=9$. Für $x < 1$ ist $f''(x) < 0$ und der Graph rechtsgekrümmt, für $x > 1$ ist $f''(x) > 0$ und der Graph linksgekrümmt.

### Schritt 7: Skizze

Für die Skizze tragen Sie die Punkte $(-1|0)$, $H(0|4)$, $W(1|2)$, $T(2|0)$ sowie zur Orientierung $(-2|-16)$ und $(3|4)$ ein. Der Graph kommt von links unten, schneidet die $x$-Achse bei $-1$, steigt zum Hochpunkt, fällt durch den Wendepunkt zum Tiefpunkt auf der $x$-Achse und steigt danach unbegrenzt.

### Häufiger Fehler in der Kurvendiskussion

Die $y$-Koordinaten von Extrem- und Wendepunkten gehören immer zur Ausgangsfunktion $f$, nicht zu $f'$ oder $f''$. Wer etwa $f''(0)=-6$ als $y$-Wert des Hochpunkts angibt, verwechselt die Prüfgröße mit dem Funktionswert $f(0)=4$.

## Zum Weiterlesen

Zur Vertiefung eignen sich Ihr Schulbuch der Oberstufe sowie Erklärvideos zur Kurvendiskussion, zum Beispiel bei Studyflix. Zum Nachprüfen eigener Rechnungen hilft ein Funktionsplotter, mit dem Sie Graph, Ableitung und berechnete Punkte vergleichen können.
