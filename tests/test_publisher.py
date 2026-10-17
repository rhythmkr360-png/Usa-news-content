import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import update_feed as news

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def sample(title='U.S. Navy announces new aircraft carrier training exercise',
           description='U.S. Navy crews conducted a scheduled carrier exercise this week, including aircraft launches and shipboard coordination.',
           link='https://www.dvidshub.net/news/1234',
           date='Sat, 17 Oct 2026 10:00:00 GMT'):
    return dict(title=title, description=description, link=link,
                sourceName='DVIDS', publishedAt=date)


class ParsingTests(unittest.TestCase):
    def test_rss_reads_entities_publisher_and_publication_date(self):
        xml = b'''<rss version="2.0"><channel><item>
          <title>U.S. Navy &amp; allies complete naval exercise</title>
          <description><![CDATA[<p>The U.S. Navy completed a training exercise with allied vessels during a planned deployment.</p>]]></description>
          <link>https://www.dvidshub.net/news/1234</link>
          <pubDate>Sat, 17 Oct 2026 10:00:00 GMT</pubDate>
          </item></channel></rss>'''
        items = news.parse_syndication(xml, 'DVIDS')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['sourceName'], 'DVIDS')
        self.assertEqual(items[0]['title'], 'U.S. Navy & allies complete naval exercise')
        self.assertEqual(items[0]['description'][:13], 'The U.S. Navy')
        result = news.candidate(items[0], NOW, timedelta(hours=72))
        self.assertEqual(result['category'], 'Navy')
        self.assertEqual(result['publishedAt'], '2026-10-17T10:00:00Z')

    def test_atom_reads_link_and_publication(self):
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom">
           <entry><title>U.S. Army announces a new training exercise</title>
           <link rel="alternate" href="https://army.mil/article/test"/>
           <published>2026-10-17T10:00:00Z</published>
           <summary>U.S. Army units began training for a planned exercise at several bases, according to the latest military announcement.</summary>
           </entry></feed>'''
        self.assertIsNotNone(news.candidate(news.parse_syndication(xml, 'Army')[0], NOW, timedelta(hours=72)))

    def test_rejects_missing_source_evidence_and_wrong_dates(self):
        item = sample(description='U.S. Navy')
        self.assertIsNone(news.candidate(item, NOW, timedelta(hours=72)))
        item = sample(date='Fri, 10 Oct 2026 10:00:00 GMT')
        self.assertIsNone(news.candidate(item, NOW, timedelta(hours=72)))
        item = sample(date='Sun, 18 Oct 2026 10:00:00 GMT')
        self.assertIsNone(news.candidate(item, NOW, timedelta(hours=72)))

    def test_rejects_unsafe_urls_and_removes_tracking_parameters(self):
        self.assertIsNone(news.https_url('http://example.com/foo'))
        self.assertIsNone(news.https_url('https://user:pw@example.com/'))
        self.assertIsNone(news.https_url('https://@example.com/'))
        self.assertIsNone(news.https_url('https://example.com:8080/foo'))
        self.assertEqual(news.https_url('https://example.com/news?id=1&utm_source=abc#fragment'),
                         'https://example.com/news?id=1')

    def test_images_only_from_dvids_hosted_news_media(self):
        def xml(url):
            return f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><item>
                <title>U.S. Navy begins naval exercise with aircraft</title>
                <description>U.S. Navy personnel completed an aircraft exercise at sea during a scheduled maritime training event.</description>
                <link>https://www.dvidshub.net/news/1234</link>
                <pubDate>Sat, 17 Oct 2026 10:00:00 GMT</pubDate>
                <media:thumbnail url="{url}"/>
                </item></channel></rss>'''.encode()
        allowed = news.parse_syndication(
            xml('https://media-cdn.dvidshub.net/photo.jpg'),
            'DVIDS U.S. Military News'
        )[0]
        self.assertEqual(
            news.candidate(allowed, NOW, timedelta(hours=72))['imageUrl'],
            'https://media-cdn.dvidshub.net/photo.jpg'
        )
        rejected = news.parse_syndication(
            xml('https://random-publisher.example/photo.jpg'),
            'DVIDS U.S. Military News'
        )[0]
        self.assertNotIn('imageUrl', news.candidate(rejected, NOW, timedelta(hours=72)))
        not_dvids = news.parse_syndication(
            xml('https://media-cdn.dvidshub.net/photo.jpg'),
            'Other Publisher'
        )[0]
        self.assertNotIn('imageUrl', news.candidate(not_dvids, NOW, timedelta(hours=72)))

    def test_invalid_format_rejected(self):
        with self.assertRaises(ValueError):
            news.parse_syndication(b'<stories><story/></stories>', 'Other')

    def test_deduplication_id_stable_under_tracking_url(self):
        a = news.candidate(sample(), NOW, timedelta(hours=72))
        b = news.candidate(sample(link='https://www.dvidshub.net/news/1234?utm_campaign=a'), NOW, timedelta(hours=72))
        self.assertEqual(a['id'], b['id'])

    def test_category_labels_match_flutter_filters(self):
        marine = sample(
            title='U.S. Marines conduct scheduled exercise at Camp Pendleton',
            description='U.S. Marine Corps personnel completed an exercise at Camp Pendleton with live-fire training and coordinated ground operations.'
        )
        technology = sample(
            title='Pentagon evaluates new missile radar technology',
            description='The Pentagon reviewed a missile radar system as part of a United States military defense technology evaluation.'
        )
        self.assertEqual(news.candidate(marine, NOW, timedelta(hours=72))['category'], 'Marine Corps')
        self.assertEqual(news.candidate(technology, NOW, timedelta(hours=72))['category'], 'Defense')

    def test_requires_unambiguous_us_military_evidence(self):
        other_country = sample(
            title='Foreign navy completes maritime military exercise',
            description='The foreign navy carried out a naval deployment with allied vessels, coordinating maritime training and ship movements.'
        )
        self.assertIsNone(news.candidate(other_country, NOW, timedelta(hours=72)))
        self.assertIsNotNone(news.candidate(sample(), NOW, timedelta(hours=72)))


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.p = patch.object(news, 'ROOT', self.root)
        self.p.start()
        self.addCleanup(self.p.stop)
        (self.root / 'data').mkdir()
        news.store_json(self.root / 'data' / 'articles.json', [])
        news.store_json(self.root / 'data' / 'seen.json', [])
        news.store_json(self.root / 'feed.json', {'version': 1, 'articles': []})

    def test_feed_pagination_and_deduplication(self):
        articles = []
        for i in range(5):
            item = news.candidate(sample(link=f'https://www.dvidshub.net/news/{i}',
                                         date=f'2026-10-17T10:{i:02d}:00Z'),
                                  NOW, timedelta(hours=72))
            articles.append(item)
        self.assertTrue(news.publish(articles, NOW, 2))
        first = news.json_file(self.root / 'feed.json', {})
        self.assertEqual(len(first['articles']), 2)
        self.assertEqual(first['nextPage'], 'pages/page-2.json')
        second = news.json_file(self.root / 'pages' / 'page-2.json', {})
        self.assertEqual(second['nextPage'], 'page-3.json')
        third = news.json_file(self.root / 'pages' / 'page-3.json', {})
        self.assertNotIn('nextPage', third)
        self.assertEqual(len(third['articles']), 1)
        self.assertFalse(news.publish(articles, NOW, 2))
        self.assertEqual(len(news.json_file(self.root / 'data' / 'articles.json', [])), 5)

    def test_expires_old_records_when_no_new_news(self):
        old = news.candidate(sample(date='2026-10-17T10:00:00Z'), NOW, timedelta(hours=72))
        news.publish([old], NOW, 2)
        much_later = NOW + timedelta(days=12)
        self.assertTrue(news.publish([], much_later, 2))
        current = news.json_file(self.root / 'feed.json', {})
        self.assertEqual(current['articles'], [])
        self.assertNotIn('nextPage', current)

    def test_dry_run_never_writes(self):
        news.store_json(self.root / 'sources.json', [{'name': 'DVIDS', 'type': 'rss', 'url': 'https://dvidshub.net/rss/news'}])
        xml = b'<rss><channel><item><title>U.S. Navy announces planned carrier exercise</title><description>U.S. Navy crews are training for a planned exercise at sea with several aircraft and vessels.</description><link>https://www.dvidshub.net/news/99</link><pubDate>Sat, 17 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>'
        with patch.object(news, 'download', return_value=xml):
            self.assertEqual(news.run(NOW, 150, dry_run=True), 0)
        self.assertEqual(news.json_file(self.root / 'feed.json', {})['articles'], [])

    def test_failures_keep_prior_published_feed(self):
        news.store_json(self.root / 'sources.json', [{'name': 'DVIDS', 'type': 'rss', 'url': 'https://dvidshub.net/rss/news'}])
        with patch.object(news, 'download', side_effect=TimeoutError('offline')):
            self.assertEqual(news.run(NOW, 150, dry_run=False), 1)
        self.assertEqual(news.json_file(self.root / 'feed.json', {}), {'version': 1, 'articles': []})

    def test_optional_ai_no_key_makes_no_network_request(self):
        batch = [news.candidate(sample(), NOW, timedelta(hours=72))]
        previous = batch[0]['summary']
        with patch.object(news, 'urlopen', side_effect=AssertionError('Network must not be called')):
            news.rewrite_with_openai(batch, api_key='', model='gpt-4.1-mini', max_ai=100)
        self.assertEqual(batch[0]['summary'], previous)


if __name__ == '__main__':
    unittest.main()

