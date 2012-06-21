#!/usr/bin/env php5
<?php

if (!isset($argv[1])) {
	throw new Exception('missing the svn url');
}

$authors = array();

$svnUrl = 'https://svn.typo3.org/' . $argv[1];
$xmlStream = shell_exec('svn log --xml ' . $svnUrl);
$xml = simplexml_load_string($xmlStream);
$log = $xml->log;

foreach($xml->logentry as $logentry) {
	$author = (string)$logentry->author;
	if (!in_array($author, $authors)) {
		$authors[] = $author;
	}
}

print(implode(chr(10), $authors) . chr(10));

?>