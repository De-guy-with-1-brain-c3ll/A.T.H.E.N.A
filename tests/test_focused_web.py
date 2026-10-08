import unittest
from unittest.mock import AsyncMock
from athena.tools.web import extract_page, ReadWebpageTool
from athena.tools._http import WebResponse

CALENDAR = '''<title>2026 calendar</title><main>
<a href="/azerbaijan"><div><h2>Azerbaijan 2026</h2><p>Race results: 1 Russell RUS 2 Verstappen VER 3 Hadjar HAD</p></div></a>
<a href="/bahrain"><div><h2>Bahrain 2026</h2><p>Grand Prix weekend October 2 to October 4. Event details.</p></div></a>
<a href="/singapore"><div><h2>Singapore 2026</h2><p>Grand Prix weekend October 9 to October 11. Event details.</p></div></a>
</main>'''

class FocusedWebTests(unittest.IsolatedAsyncioTestCase):
    def test_missing_bahrain_results_never_borrow_azerbaijan_podium(self):
        data=extract_page(CALENDAR,'https://example.com/calendar',query='Bahrain results 2026')
        self.assertTrue(data['focus_matched'])
        self.assertIn('Bahrain', data['text'])
        for unrelated in ('Russell','Hadjar','Azerbaijan','Singapore'):
            self.assertNotIn(unrelated,data['text'])
        self.assertEqual(data['links'][0]['url'],'https://example.com/bahrain')
        self.assertEqual(data['sections'][0]['source_url'],'https://example.com/bahrain')
        self.assertFalse(any('Russell' in row['title'] or 'azerbaijan' in row['url'] for row in data['links']))

    def test_no_match_returns_links_not_an_unrelated_answer(self):
        data=extract_page(CALENDAR,'https://example.com',query='Monaco')
        self.assertFalse(data['focus_matched'])
        self.assertEqual(data['text'],'')
        self.assertTrue(data['links'])

    def test_results_navigation_survives_text_cleanup(self):
        html='<nav><a href="/bahrain/results">Race results</a>Noise</nav>'+CALENDAR
        data=extract_page(html,'https://example.com',query='Bahrain')
        self.assertIn('https://example.com/bahrain/results', [row['url'] for row in data['links']])
        self.assertNotIn('Noise',data['text'])

    def test_relevant_link_is_not_lost_after_30_other_links(self):
        html='<main>'+''.join(f'<a href="/other/{i}">Other event {i}</a>' for i in range(50))+CALENDAR+'</main>'
        data=extract_page(html,'https://example.com',query='Bahrain')
        self.assertEqual(data['links'][0]['url'],'https://example.com/bahrain')

    def test_headings_and_table_rows_preserve_relationships(self):
        html='<main><h2>Bahrain results</h2><table><tr><th>Position</th><th>Driver</th></tr><tr><td>1</td><td>Driver A</td></tr></table><h2>Azerbaijan results</h2><table><tr><td>1</td><td>Driver B</td></tr></table></main>'
        data=extract_page(html,'https://example.com',query='Bahrain')
        self.assertIn('ROW 2 | 1 | Driver A',data['text'])
        self.assertNotIn('Driver B',data['text'])
        self.assertEqual(len(data['sections']),1)

    async def test_cache_and_refresh_without_extra_model_calls(self):
        http=AsyncMock()
        http.get.return_value=WebResponse('https://example.com',CALENDAR.encode(),'text/html')
        reader=ReadWebpageTool(http)
        args={'url':'https://example.com','query':'Bahrain'}
        first=await reader.execute(args)
        first.data['text']='tampered by caller'
        second=await reader.execute(args)
        self.assertTrue(second.data['cache_hit'])
        self.assertNotIn('tampered',second.data['text'])
        self.assertEqual(http.get.await_count,1)
        await reader.execute({**args,'refresh':True})
        self.assertEqual(http.get.await_count,2)

    def test_without_query_cards_still_have_separate_boundaries(self):
        data=extract_page(CALENDAR,'https://example.com')
        self.assertEqual(len(data['sections']),3)
        self.assertIn('[END SECTION 1]',data['text'])
