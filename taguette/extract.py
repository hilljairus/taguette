from bs4 import BeautifulSoup, NavigableString


def find_pos(node, offset, after):
    """Find a position in an HTML document.
    """
    indices = []
    while True:
        if getattr(node, 'contents', None):
            indices.append(0)
            node = node.contents[0]
        else:
            if isinstance(node, NavigableString):
                nb = len(node.string.encode('utf-8'))
                if (after and nb > offset) or (not after and nb >= offset):
                    break
                else:
                    offset -= nb
            while not node.next_sibling:
                indices.pop()
                node = node.parent
            indices[-1] += 1
            node = node.next_sibling
    return node, offset, indices


def delete_left(node, indices):
    """Delete the left part of an HTML tree.
    """
    for idx in indices:
        del node.contents[:idx]
        node = node.contents[0]


def delete_right(node, indices):
    """Delete the right part of an HTML tree.
    """
    for idx in indices:
        del node.contents[idx + 1:]
        node = node.contents[idx]


def extract(html, start, end):
    """Extract a snippet out of an HTML document.

    Locations are computed over UTF-8 bytes, and doesn't count HTML tags.

    Extraction is aware of tags, so:

    >>> '<p><u>Hello</u> there <i>World</i></p>'[17:27]
    'here <i>Wo'
    >>> extract('<p><u>Hello</u> there <i>World</i></p>', 7, 14)
    '<p>here <i>Wo</i></p>'
    """
    soup = BeautifulSoup(html, 'html5lib')

    # Trim the right side first, because that doesn't mess our start position
    if end is not None:
        e = find_pos(soup, end, False)
        e[0].replace_with(NavigableString(e[0].string[:e[1]]))
        delete_right(soup, e[2])

    # Trim the left side
    if start is not None:
        s = find_pos(soup, start, True)
        s[0].replace_with(NavigableString(s[0].string[s[1]:]))
        delete_left(soup, s[2])

    # Remove everything but body
    body = soup.body
    soup.clear()
    soup.append(body)

    # Remove the body tag itself to only have the contents
    soup.body.unwrap()

    # Back to text
    return str(soup)


def highlight(html, highlights):
    """Highlight part of an HTML documents.

    :param highlights: Iterable of (start, end) pairs, which are computed over
        UTF-8 bytes and don't count HTML tags
    """
    highlights = iter(highlights)
    soup = BeautifulSoup(html, 'html5lib')

    pos = 0
    node = soup
    highlighting = False
    try:
        start, end = next(highlights)
        print("first pair: %d, %d" % (start, end))
        while True:
            print("  pos = %d" % pos)
            if getattr(node, 'contents', None):
                print("  going down")
                node = node.contents[0]
            else:
                if isinstance(node, NavigableString):
                    nb = len(node.string.encode('utf-8'))
                    print("  found string, len = %d" % nb)
                    while True:
                        from copy import copy
                        soup2 = copy(soup)
                        body = soup2.body
                        soup2.clear()
                        soup2.append(body)
                        soup2.body.unwrap()
                        print('    ' + str(soup2)
                              .replace('<span class="highlight">', '{')
                              .replace('</span>', '}'))

                        if not highlighting and start == pos:
                            print("    case 1")
                            highlighting = True
                        elif not highlighting and pos + nb > start:
                            print("    case 2")
                            parent = node.parent
                            left = node.string[:start - pos]
                            right = node.string[start - pos:]
                            idx = parent.index(node)
                            node.replace_with(NavigableString(left))
                            node = NavigableString(right)
                            parent.insert(idx + 1, node)
                            nb -= start - pos
                            pos = start
                            # Code below will do the actual highlighting
                            highlighting = True
                        elif highlighting and pos + nb <= end:
                            print("    case 3")
                            newnode = soup.new_tag(
                                'span',
                                attrs={'class': 'highlight'},
                            )
                            node.replace_with(newnode)
                            newnode.append(node)
                            node = newnode
                            if pos + nb == end:
                                print("    exact end")
                                highlighting = False
                                start, end = next(highlights)
                                print("    next pair: %d, %d" % (start, end))
                            break
                        elif highlighting:
                            print("    case 4")
                            parent = node.parent
                            left = node.string[:end - pos]
                            rest = node.string[end - pos:]
                            idx = parent.index(node)
                            newnode = NavigableString(left)
                            node.replace_with(newnode)
                            node = newnode
                            newnode = soup.new_tag(
                                'span',
                                attrs={'class': 'highlight'},
                            )
                            node.replace_with(newnode)
                            newnode.append(node)
                            node = NavigableString(rest)
                            parent.insert(idx + 1, node)
                            nb -= end - pos
                            pos = end
                            highlighting = False
                            start, end = next(highlights)
                            print("    next pair: %d, %d" % (start, end))
                        else:
                            break
                    from copy import copy
                    soup2 = copy(soup)
                    body = soup2.body
                    soup2.clear()
                    soup2.append(body)
                    soup2.body.unwrap()
                    print('    ' + str(soup2)
                          .replace('<span class="highlight">', '{')
                          .replace('</span>', '}'))
                    print("  end loop")

                    pos += nb
                while not node.next_sibling:
                    if not node.parent:
                        raise StopIteration
                    print("  going up")
                    node = node.parent
                print("  going right")
                node = node.next_sibling
    except StopIteration:
        print("done")
        # Remove everything but body
        body = soup.body
        soup.clear()
        soup.append(body)

        # Remove the body tag itself to only have the contents
        soup.body.unwrap()

        # Back to text
        return str(soup)
